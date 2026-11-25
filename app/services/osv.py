"""OSV.dev vulnerability checking for the coding section.

* Dependency extraction from manifests (requirements*.txt, pyproject.toml,
  Pipfile, poetry.lock, package.json, package-lock.json) and from new
  import / require statements in .py / .js / .ts files.
* Unpinned packages are resolved to the version that would be installed
  (PyPI JSON API / npm registry) before checking.
* OSV.dev /v1/querybatch + /v1/vulns/{id} (both cached in Redis).
* Recommendations: first fixed version above the current one, otherwise a
  curated safe alternative package.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sys
import tomllib
from dataclasses import asdict, dataclass

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from app.core.state import http, redis

log = logging.getLogger("deminic.osv")

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/{}"
PYPI_JSON = "https://pypi.org/pypi/{}/json"
NPM_REG = "https://registry.npmjs.org/{}"
MAX_DEPS = 1500


class ScannerUnavailable(Exception):
    """OSV.dev (or a package registry) could not be reached."""


@dataclass(frozen=True)
class Dep:
    ecosystem: str  # "PyPI" | "npm"
    name: str
    version: str | None = None  # pinned / resolved version
    spec: str = ""  # original constraint text
    source: str = ""  # manifest / import origin

    @property
    def key(self) -> tuple[str, str]:
        return self.ecosystem, self.name

    def with_version(self, v: str | None) -> "Dep":
        return Dep(self.ecosystem, self.name, v, self.spec, self.source)


def norm_pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


# --------------------------------------------------------------------------- curated maps
IMPORT_TO_PYPI = {
    "cv2": "opencv-python", "PIL": "pillow", "yaml": "pyyaml", "sklearn": "scikit-learn", "skimage": "scikit-image",
    "bs4": "beautifulsoup4", "jwt": "pyjwt", "Crypto": "pycryptodome", "Cryptodome": "pycryptodomex",
    "dateutil": "python-dateutil", "dotenv": "python-dotenv", "magic": "python-magic", "serial": "pyserial",
    "usb": "pyusb", "OpenSSL": "pyopenssl", "attr": "attrs", "jose": "python-jose", "multipart": "python-multipart",
    "docx": "python-docx", "pptx": "python-pptx", "fitz": "pymupdf", "MySQLdb": "mysqlclient", "psycopg2": "psycopg2",
    "telegram": "python-telegram-bot", "discord": "discord.py", "git": "gitpython", "websocket": "websocket-client",
    "zmq": "pyzmq", "google": "protobuf", "socketio": "python-socketio", "engineio": "python-engineio",
    "Levenshtein": "python-levenshtein", "slugify": "python-slugify", "jinja2": "jinja2", "markdown": "markdown",
    "nacl": "pynacl", "gi": "pygobject", "win32api": "pywin32", "win32con": "pywin32", "pkg_resources": "setuptools",
    "lxml": "lxml", "ldap": "python-ldap", "Image": "pillow", "fastapi": "fastapi", "starlette": "starlette",
    "pydantic": "pydantic", "sqlalchemy": "sqlalchemy", "flask": "flask", "django": "django", "requests": "requests",
}

ALTERNATIVES = {
    ("PyPI", "pycrypto"): "pycryptodome (drop-in, maintained)",
    ("PyPI", "python-jose"): "joserfc or PyJWT",
    ("PyPI", "pyjwt"): "pyjwt>=2.4 (or joserfc)",
    ("PyPI", "m2crypto"): "cryptography",
    ("PyPI", "pyopenssl"): "cryptography",
    ("PyPI", "ecdsa"): "cryptography",
    ("PyPI", "rsa"): "cryptography",
    ("PyPI", "httplib2"): "httpx",
    ("PyPI", "simplejson"): "json (standard library)",
    ("PyPI", "pyyaml"): "pyyaml>=6 with yaml.safe_load",
    ("PyPI", "lxml"): "defusedxml for untrusted XML",
    ("PyPI", "xmltodict"): "defusedxml + xmltodict>=0.13",
    ("PyPI", "py"): "remove it (pytest no longer needs 'py')",
    ("PyPI", "pickle5"): "json / msgpack (never unpickle untrusted data)",
    ("PyPI", "jinja2"): "jinja2>=3.1.4",
    ("PyPI", "flask-cors"): "flask-cors>=4.0.1",
    ("npm", "request"): "undici / node-fetch / axios (request is deprecated)",
    ("npm", "request-promise"): "got or undici",
    ("npm", "node-uuid"): "uuid",
    ("npm", "moment"): "date-fns or dayjs",
    ("npm", "jsonwebtoken"): "jsonwebtoken>=9 or jose",
    ("npm", "node-serialize"): "JSON.parse / JSON.stringify (never deserialize untrusted input)",
    ("npm", "crypto-js"): "Web Crypto API / node:crypto",
    ("npm", "express-fileupload"): "multer",
    ("npm", "underscore"): "lodash-es",
    ("npm", "event-stream"): "remove it (3.3.6 was compromised)",
    ("npm", "colors"): "picocolors or chalk",
    ("npm", "faker"): "@faker-js/faker",
    ("npm", "node-ipc"): "remove it (protestware)",
    ("npm", "xmldom"): "@xmldom/xmldom",
    ("npm", "querystring"): "URLSearchParams (built in)",
    ("npm", "vm2"): "isolated-vm (vm2 is discontinued with sandbox escapes)",
    ("npm", "shelljs"): "execa or node:child_process",
    ("npm", "lodash"): "lodash>=4.17.21 or lodash-es",
    ("npm", "minimist"): "minimist>=1.2.6 or yargs",
    ("npm", "axios"): "axios>=1.7.4",
    ("npm", "marked"): "marked>=4.0.10 + DOMPurify",
}

NODE_BUILTINS = set("""assert async_hooks buffer child_process cluster console constants crypto dgram diagnostics_channel dns
domain events fs http http2 https inspector module net os path perf_hooks process punycode querystring readline repl stream
string_decoder sys timers tls trace_events tty url util v8 vm wasi worker_threads zlib test""".split())
STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


# --------------------------------------------------------------------------- extraction
def _caret_tilde_to_pep440(spec: str) -> str:
    spec = spec.strip()
    m = re.fullmatch(r"([\^~])\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", spec)
    if not m:
        return spec
    op, a, b, c = m.group(1), int(m.group(2)), int(m.group(3) or 0), int(m.group(4) or 0)
    low = f"{a}.{b}.{c}"
    if op == "~":
        return f">={low},<{a}.{b + 1}.0"
    if a > 0:
        return f">={low},<{a + 1}.0.0"
    if b > 0:
        return f">={low},<0.{b + 1}.0"
    return f">={low},<0.0.{c + 1}"


def _pep508(text: str, source: str) -> Dep | None:
    try:
        req = Requirement(text)
    except InvalidRequirement:
        return None
    pinned = None
    for s in req.specifier:
        if s.operator in ("==", "===") and "*" not in s.version:
            pinned = s.version
    return Dep("PyPI", norm_pypi(req.name), pinned, str(req.specifier), source)


def parse_requirements(text: str, source: str) -> list[Dep]:
    out = []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if not line or line.startswith(("#", "-", "--")) or "://" in line or line.startswith((".", "/", "git+")):
            continue
        d = _pep508(line, source)
        if d:
            out.append(d)
    return out


def _poetry_dep(name: str, val, source: str) -> Dep | None:
    if name.lower() == "python":
        return None
    spec = val.get("version", "") if isinstance(val, dict) else str(val)
    if isinstance(val, dict) and ("git" in val or "path" in val or "url" in val):
        return None
    spec = spec.strip()
    if spec in ("", "*"):
        return Dep("PyPI", norm_pypi(name), None, "", source)
    if re.fullmatch(r"\d+(\.\d+)*", spec):
        return Dep("PyPI", norm_pypi(name), spec, f"=={spec}", source)
    return Dep("PyPI", norm_pypi(name), None, _caret_tilde_to_pep440(spec), source)


def parse_pyproject(text: str, source: str) -> list[Dep]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    out: list[Dep] = []
    proj = data.get("project") or {}
    for s in proj.get("dependencies") or []:
        if isinstance(s, str) and (d := _pep508(s, source)):
            out.append(d)
    for group in (proj.get("optional-dependencies") or {}).values():
        for s in group or []:
            if isinstance(s, str) and (d := _pep508(s, source)):
                out.append(d)
    for group in (data.get("dependency-groups") or {}).values():
        for s in group or []:
            if isinstance(s, str) and (d := _pep508(s, source)):
                out.append(d)
    poetry = (data.get("tool") or {}).get("poetry") or {}
    for name, val in (poetry.get("dependencies") or {}).items():
        if d := _poetry_dep(name, val, source):
            out.append(d)
    for g in (poetry.get("group") or {}).values():
        for name, val in ((g or {}).get("dependencies") or {}).items():
            if d := _poetry_dep(name, val, source):
                out.append(d)
    return out


def parse_pipfile(text: str, source: str) -> list[Dep]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    out = []
    for sect in ("packages", "dev-packages"):
        for name, val in (data.get(sect) or {}).items():
            spec = val.get("version", "*") if isinstance(val, dict) else str(val)
            if isinstance(val, dict) and ("git" in val or "path" in val or "file" in val):
                continue
            spec = "" if spec.strip() == "*" else spec.strip()
            d = _pep508(f"{name}{spec}", source) if spec else Dep("PyPI", norm_pypi(name), None, "", source)
            if d:
                out.append(d)
    return out


def parse_poetry_lock(text: str, source: str) -> list[Dep]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    return [Dep("PyPI", norm_pypi(p["name"]), str(p["version"]), f"=={p['version']}", source)
            for p in data.get("package") or [] if p.get("name") and p.get("version")]


def _npm_dep(name: str, spec: str, source: str) -> Dep | None:
    spec = str(spec or "").strip()
    if not name or spec.startswith(("git", "http", "file:", "link:", "workspace:", "github:", "/", ".")):
        return None
    if re.fullmatch(r"[\w.-]+/[\w.-]+(#.*)?", spec):  # GitHub shorthand "user/repo"
        return None
    if spec.startswith("npm:"):  # alias: "npm:real-name@^1.2"
        body = spec[4:]
        at = body.rfind("@")
        if at > 0:
            name, spec = body[:at], body[at + 1:]
        else:
            name, spec = body, ""
    if re.fullmatch(r"v?\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?", spec):
        return Dep("npm", name.lower(), spec.lstrip("v"), spec, source)
    return Dep("npm", name.lower(), None, spec, source)


def parse_package_json(text: str, source: str) -> list[Dep]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for sect in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in (data.get(sect) or {}).items():
            if d := _npm_dep(name, spec, source):
                out.append(d)
    return out


def parse_package_lock(text: str, source: str) -> list[Dep]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for path, meta in (data.get("packages") or {}).items():
        if not path or not isinstance(meta, dict) or not meta.get("version"):
            continue
        name = meta.get("name") or path.rsplit("node_modules/", 1)[-1]
        out.append(Dep("npm", name.lower(), str(meta["version"]), str(meta["version"]), source))
    if not out:  # lockfile v1
        for name, meta in (data.get("dependencies") or {}).items():
            if isinstance(meta, dict) and meta.get("version"):
                out.append(Dep("npm", name.lower(), str(meta["version"]), str(meta["version"]), source))
    return out


def python_imports(text: str, source: str, local_names: set[str] | None = None, pins: dict | None = None) -> list[Dep]:
    local_names = local_names or set()
    pins = pins or {}
    mods: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("import "):
            for part in line[7:].split("#", 1)[0].split(","):
                name = part.strip().split(" as ")[0].strip()
                if name:
                    mods.append(name.split(".")[0])
        elif line.startswith("from ") and " import " in line:
            mod = line[5:].split(" import ", 1)[0].strip()
            if mod and not mod.startswith("."):
                mods.append(mod.split(".")[0])
    out, seen = [], set()
    for mod in mods:
        if not re.fullmatch(r"[A-Za-z_]\w*", mod) or mod in STDLIB or mod in local_names or mod in seen:
            continue
        seen.add(mod)
        pkg = norm_pypi(IMPORT_TO_PYPI.get(mod, mod))
        pinned = pins.get(("PyPI", pkg))
        out.append(Dep("PyPI", pkg, pinned, f"=={pinned}" if pinned else "", f"{source} (import {mod})"))
    return out


_JS_RE = re.compile(r"""(?:\bimport\s+(?:[\w*{}\s,$]+?\s+from\s+)?|\bexport\s+[\w*{}\s,$]+?\s+from\s+|\brequire\s*\(\s*|\bimport\s*\(\s*)['"]([^'"\n]+)['"]""")


def js_imports(text: str, source: str, pins: dict | None = None) -> list[Dep]:
    pins = pins or {}
    out, seen = [], set()
    for spec in _JS_RE.findall(text):
        if spec.startswith((".", "/", "~", "@/", "#", "http:", "https:", "data:", "node:")):
            continue
        parts = spec.split("/")
        name = "/".join(parts[:2]) if spec.startswith("@") and len(parts) > 1 else parts[0]
        name = name.lower()
        if not name or name in NODE_BUILTINS or name in seen or not re.fullmatch(r"(@[a-z0-9._~-]+/)?[a-z0-9._~-]+", name):
            continue
        seen.add(name)
        pinned = pins.get(("npm", name))
        out.append(Dep("npm", name, pinned, pinned or "", f"{source} (import {name})"))
    return out


def deps_from_text(text: str, filename: str, *, local_names: set[str] | None = None, pins: dict | None = None) -> list[Dep]:
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
    ext = "." + base.rsplit(".", 1)[-1] if "." in base else ""
    if base.endswith((".txt", ".in")) and ("requirement" in base or "constraint" in base):
        return parse_requirements(text, base)
    if base == "pyproject.toml":
        return parse_pyproject(text, base)
    if base == "pipfile":
        return parse_pipfile(text, base)
    if base == "poetry.lock":
        return parse_poetry_lock(text, base)
    if base == "package.json":
        return parse_package_json(text, base)
    if base in ("package-lock.json", "npm-shrinkwrap.json"):
        return parse_package_lock(text, base)
    if ext in (".py", ".pyw"):
        return python_imports(text, base, local_names, pins)
    if ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte"):
        return js_imports(text, base, pins)
    return []


MANIFEST_NAMES = ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "Pipfile", "poetry.lock", "package.json",
                  "package-lock.json")


def is_manifest(name: str) -> bool:
    b = name.lower()
    return b in {m.lower() for m in MANIFEST_NAMES} or (b.endswith(".txt") and "requirement" in b)


def parse_package_list(text: str, ecosystem_hint: str = "") -> list[Dep]:
    """Free-form "requests==2.19.0, flask, lodash@4.17.15" -> deps (used by the AI tool)."""
    out = []
    for tok in re.split(r"[,\n;]+|\s{2,}", text or ""):
        tok = tok.strip().strip("`'\"")
        if not tok:
            continue
        eco = ecosystem_hint if ecosystem_hint in ("PyPI", "npm") else ""
        if "@" in tok[1:] and "==" not in tok:  # npm style name@version
            at = tok.rfind("@")
            name, ver = tok[:at], tok[at + 1:]
            out.append(Dep("npm", name.lower(), ver if re.fullmatch(r"\d+\.\d+\.\d+.*", ver) else None, ver, "chat"))
            continue
        m = re.match(r"^([A-Za-z0-9@][A-Za-z0-9._/@-]*)\s*(?:(==|>=|<=|~=|>|<| )\s*v?([0-9][0-9A-Za-z.\-]*))?$", tok)
        if not m:
            continue
        name, op, ver = m.group(1), m.group(2), m.group(3)
        if not eco:
            eco = "npm" if name.startswith("@") or name.lower() in ("express", "lodash", "react", "axios", "moment",
                                                                     "jsonwebtoken", "request", "vue", "next") else "PyPI"
        pinned = ver if ver and op in ("==", " ", None) else None
        if eco == "PyPI":
            out.append(Dep("PyPI", norm_pypi(name), pinned, f"{op or ''}{ver or ''}".strip(), "chat"))
        else:
            out.append(Dep("npm", name.lower(), pinned, ver or "", "chat"))
    return out


# --------------------------------------------------------------------------- semver (npm)
_SV = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def sv(v: str) -> tuple | None:
    m = _SV.match((v or "").strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0), m.group(4) or "")


def sv_key(t: tuple) -> tuple:
    return t[0], t[1], t[2], 0 if t[3] else 1, t[3]


def _cmp_ok(v: tuple, op: str, ref: tuple) -> bool:
    a, b = sv_key(v), sv_key(ref)
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b, "=": a == b}[op]


def _expand(token: str) -> list[tuple[str, tuple]]:
    """One npm comparator token -> list of (op, version) constraints."""
    token = token.strip()
    if token in ("", "*", "x", "X", "latest"):
        return []
    m = re.match(r"^(\^|~|>=|<=|>|<|=)?\s*v?([0-9xX*]+)(?:\.([0-9xX*]+))?(?:\.([0-9xX*]+))?(?:-([0-9A-Za-z.-]+))?$", token)
    if not m:
        return [("=", (999999, 0, 0, ""))]  # unparsable -> matches nothing
    op = m.group(1) or ""
    parts = [m.group(2), m.group(3), m.group(4)]
    wild = [p is None or p in ("x", "X", "*") for p in parts]
    nums = [0 if w else int(p) for p, w in zip(parts, wild)]
    pre = m.group(5) or ""
    a, b, c = nums
    if op == "^":
        low = (a, b, c, pre)
        if a > 0 or wild[1]:
            high = (a + 1, 0, 0, "")
        elif b > 0 or wild[2]:
            high = (0, b + 1, 0, "")
        else:
            high = (0, 0, c + 1, "")
        return [(">=", low), ("<", high)]
    if op == "~":
        low = (a, b, c, pre)
        high = (a + 1, 0, 0, "") if wild[1] else (a, b + 1, 0, "")
        return [(">=", low), ("<", high)]
    if op in ("", "=") and (wild[1] or wild[2]):
        if wild[0]:
            return []
        if wild[1]:
            return [(">=", (a, 0, 0, "")), ("<", (a + 1, 0, 0, ""))]
        return [(">=", (a, b, 0, "")), ("<", (a, b + 1, 0, ""))]
    return [(op or "=", (a, b, c, pre))]


def satisfies(version: str, rng: str) -> bool:
    v = sv(version)
    if v is None:
        return False
    rng = (rng or "").strip()
    for alt in rng.split("||"):
        alt = alt.strip()
        hy = re.match(r"^(\S+)\s+-\s+(\S+)$", alt)
        if hy:
            cons = [(">=", sv(hy.group(1)) or (0, 0, 0, "")), ("<=", sv(hy.group(2)) or (999999, 0, 0, ""))]
        else:
            cons = []
            for tok in re.findall(r"(?:[<>]=?|=|\^|~)?\s*[^\s<>=^~]+", alt):
                cons += _expand(tok)
        if v[3] and not any(c[1][3] and c[1][:3] == v[:3] for c in cons):
            continue  # prereleases only match comparators on the same tuple
        if all(_cmp_ok(v, op, ref) for op, ref in cons):
            return True
    return False


# --------------------------------------------------------------------------- registries
async def _cached_json(key: str, url: str, *, headers: dict | None = None, ttl: int = 6 * 3600):
    hit = await redis.get(key)
    if hit:
        return None if hit == "null" else json.loads(hit)
    try:
        r = await http().get(url, headers=headers or {}, timeout=20, follow_redirects=True)
    except Exception as e:  # noqa: BLE001
        raise ScannerUnavailable(f"{url.split('/')[2]} unreachable ({type(e).__name__})") from e
    if r.status_code == 404:
        await redis.set(key, "null", ex=3600)
        return None
    if r.status_code >= 400:
        raise ScannerUnavailable(f"{url.split('/')[2]} HTTP {r.status_code}")
    data = r.json()
    return data, ttl


async def pypi_versions(name: str) -> tuple[str | None, list[str]] | None:
    key = f"reg:pypi:{name}"
    hit = await redis.get(key)
    if hit:
        return None if hit == "null" else tuple(json.loads(hit))  # type: ignore[return-value]
    res = await _cached_json(key, PYPI_JSON.format(name))
    if res is None:
        return None
    data, ttl = res
    latest = (data.get("info") or {}).get("version")
    versions = [v for v, files in (data.get("releases") or {}).items() if files] or ([latest] if latest else [])
    await redis.set(key, json.dumps([latest, versions]), ex=ttl)
    return latest, versions


async def npm_versions(name: str) -> tuple[str | None, list[str]] | None:
    key = f"reg:npm:{name}"
    hit = await redis.get(key)
    if hit:
        return None if hit == "null" else tuple(json.loads(hit))  # type: ignore[return-value]
    res = await _cached_json(key, NPM_REG.format(name.replace("/", "%2F")),
                             headers={"Accept": "application/vnd.npm.install-v1+json"})
    if res is None:
        return None
    data, ttl = res
    latest = (data.get("dist-tags") or {}).get("latest")
    versions = list((data.get("versions") or {}).keys())
    await redis.set(key, json.dumps([latest, versions]), ex=ttl)
    return latest, versions


async def resolve(dep: Dep, verify: bool = False) -> tuple[Dep, bool]:
    """Returns (dep with version, exists_on_registry). ``verify`` also checks that
    pinned packages exist (catches typos / hallucinated packages)."""
    if dep.version:
        if verify:
            info = await (pypi_versions(dep.name) if dep.ecosystem == "PyPI" else npm_versions(dep.name))
            return dep, info is not None
        return dep, True
    if dep.ecosystem == "PyPI":
        info = await pypi_versions(dep.name)
        if info is None:
            return dep, False
        latest, versions = info
        try:
            spec = SpecifierSet(dep.spec) if dep.spec else None
        except InvalidSpecifier:
            spec = None
        if spec:
            ok = []
            for v in versions:
                try:
                    pv = Version(v)
                except InvalidVersion:
                    continue
                if not pv.is_prerelease and pv in spec:
                    ok.append(pv)
            if ok:
                return dep.with_version(str(max(ok))), True
        return dep.with_version(latest), True
    info = await npm_versions(dep.name)
    if info is None:
        return dep, False
    latest, versions = info
    if dep.spec and dep.spec not in ("*", "latest", "x"):
        ok = [v for v in versions if satisfies(v, dep.spec)]
        if ok:
            return dep.with_version(max(ok, key=lambda x: sv_key(sv(x)))), True
    return dep.with_version(latest), True


# --------------------------------------------------------------------------- OSV
def _cvss3_score(vector: str) -> float | None:
    try:
        parts = dict(p.split(":", 1) for p in vector.split("/")[1:])
        av = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}[parts["AV"]]
        ac = {"L": 0.77, "H": 0.44}[parts["AC"]]
        scope_changed = parts["S"] == "C"
        pr = {"N": 0.85, "L": 0.68 if scope_changed else 0.62, "H": 0.5 if scope_changed else 0.27}[parts["PR"]]
        ui = {"N": 0.85, "R": 0.62}[parts["UI"]]
        cia = {"H": 0.56, "L": 0.22, "N": 0.0}
        iss = 1 - (1 - cia[parts["C"]]) * (1 - cia[parts["I"]]) * (1 - cia[parts["A"]])
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if scope_changed else 6.42 * iss
        expl = 8.22 * av * ac * pr * ui
        if impact <= 0:
            return 0.0
        base = min(1.08 * (impact + expl), 10) if scope_changed else min(impact + expl, 10)
        return math.ceil(base * 10 - 1e-9) / 10
    except (KeyError, ValueError):
        return None


def _severity_label(score: float | None) -> str:
    if score is None:
        return "UNKNOWN"
    if score >= 9:
        return "CRITICAL"
    if score >= 7:
        return "HIGH"
    if score >= 4:
        return "MEDIUM"
    if score > 0:
        return "LOW"
    return "NONE"


SEV_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "MODERATE": 2, "LOW": 1, "NONE": 0, "UNKNOWN": 1}


async def _osv_ids(deps: list[Dep]) -> dict[tuple, list[str]]:
    out: dict[tuple, list[str]] = {}
    todo: list[Dep] = []
    for d in deps:
        hit = await redis.get(f"osv:q:{d.ecosystem}:{d.name}:{d.version}")
        if hit is not None:
            out[(d.ecosystem, d.name, d.version)] = json.loads(hit)
        else:
            todo.append(d)
    for i in range(0, len(todo), 500):
        chunk = todo[i : i + 500]
        body = {"queries": [{"package": {"name": d.name, "ecosystem": d.ecosystem}, "version": d.version} for d in chunk]}
        try:
            r = await http().post(OSV_BATCH, json=body, timeout=40)
        except Exception as e:  # noqa: BLE001
            raise ScannerUnavailable(f"OSV.dev unreachable ({type(e).__name__})") from e
        if r.status_code >= 400:
            raise ScannerUnavailable(f"OSV.dev HTTP {r.status_code}")
        results = r.json().get("results") or []
        for d, res in zip(chunk, results):
            ids = [v["id"] for v in (res or {}).get("vulns") or [] if v.get("id")]
            out[(d.ecosystem, d.name, d.version)] = ids
            await redis.set(f"osv:q:{d.ecosystem}:{d.name}:{d.version}", json.dumps(ids), ex=6 * 3600)
    return out


async def vuln_details(vid: str) -> dict:
    key = f"osv:v:{vid}"
    hit = await redis.get(key)
    if hit:
        return json.loads(hit)
    try:
        r = await http().get(OSV_VULN.format(vid), timeout=20)
        data = r.json() if r.status_code == 200 else {"id": vid}
    except Exception:  # noqa: BLE001
        data = {"id": vid}
    await redis.set(key, json.dumps(data), ex=24 * 3600)
    return data


def _version_key(eco: str, v: str):
    if eco == "PyPI":
        try:
            return (0, Version(v))
        except InvalidVersion:
            return (1, v)
    t = sv(v)
    return (0, sv_key(t)) if t else (1, v)


def _summarise(dep: Dep, vuln: dict) -> dict:
    sev, score = None, None
    ds = vuln.get("database_specific") or {}
    if isinstance(ds.get("severity"), str):
        sev = ds["severity"].upper()
    for s in vuln.get("severity") or []:
        if str(s.get("type", "")).startswith("CVSS_V3"):
            score = _cvss3_score(s.get("score", ""))
    if score is not None and (sev is None or sev == "UNKNOWN"):
        sev = _severity_label(score)
    fixed: list[str] = []
    for aff in vuln.get("affected") or []:
        pkg = aff.get("package") or {}
        pname = norm_pypi(pkg.get("name", "")) if dep.ecosystem == "PyPI" else str(pkg.get("name", "")).lower()
        if pname != dep.name or pkg.get("ecosystem") != dep.ecosystem:
            continue
        for rng in aff.get("ranges") or []:
            for ev in rng.get("events") or []:
                if ev.get("fixed"):
                    fixed.append(str(ev["fixed"]))
    fixed = sorted(set(fixed), key=lambda v: _version_key(dep.ecosystem, v))
    return {
        "id": vuln.get("id"),
        "aliases": (vuln.get("aliases") or [])[:4],
        "summary": (vuln.get("summary") or (vuln.get("details") or "")[:220] or "No summary").strip(),
        "severity": sev or "UNKNOWN",
        "score": score,
        "fixed": fixed[:6],
        "url": f"https://osv.dev/vulnerability/{vuln.get('id')}",
    }


async def check(deps: list[Dep]) -> list[dict]:
    """Query OSV for resolved deps -> findings (only deps with vulnerabilities)."""
    deps = [d for d in deps if d.version][:MAX_DEPS]
    if not deps:
        return []
    ids_map = await _osv_ids(deps)
    all_ids = sorted({i for ids in ids_map.values() for i in ids})
    sem = asyncio.Semaphore(8)

    async def _one(vid):
        async with sem:
            return vid, await vuln_details(vid)

    details = dict(await asyncio.gather(*(_one(v) for v in all_ids))) if all_ids else {}
    findings = []
    for d in deps:
        ids = ids_map.get((d.ecosystem, d.name, d.version)) or []
        if not ids:
            continue
        vulns = [_summarise(d, details.get(i, {"id": i})) for i in ids]
        vulns.sort(key=lambda v: -SEV_RANK.get(v["severity"], 1))
        cur = _version_key(d.ecosystem, d.version or "0")
        fixes = sorted({f for v in vulns for f in v["fixed"]}, key=lambda v: _version_key(d.ecosystem, v))
        above = [f for f in fixes if _version_key(d.ecosystem, f) > cur]
        # a version that fixes ALL listed vulns = the highest of the minimal fixes per vuln
        per_vuln_min = []
        for v in vulns:
            cand = [f for f in v["fixed"] if _version_key(d.ecosystem, f) > cur]
            if cand:
                per_vuln_min.append(min(cand, key=lambda x: _version_key(d.ecosystem, x)))
        fix_all = max(per_vuln_min, key=lambda x: _version_key(d.ecosystem, x)) if per_vuln_min and len(per_vuln_min) == len(vulns) else None
        alt = ALTERNATIVES.get((d.ecosystem, d.name))
        if fix_all:
            rec = f"Upgrade {d.name} to {fix_all} or newer" + (f" (alternative: {alt})" if alt else "")
        elif above:
            rec = f"Upgrade {d.name} to at least {above[0]}; some advisories have no fix yet" + (f" - consider {alt}" if alt else "")
        elif alt:
            rec = f"No fixed version - switch to {alt}"
        else:
            rec = f"No fixed version published - avoid {d.name} {d.version} or isolate it"
        findings.append({
            "ecosystem": d.ecosystem, "name": d.name, "version": d.version, "spec": d.spec, "source": d.source,
            "vulns": vulns[:12], "count": len(vulns),
            "max_severity": max((v["severity"] for v in vulns), key=lambda s: SEV_RANK.get(s, 1)),
            "fix_version": fix_all or (above[0] if above else None), "alternative": alt, "recommendation": rec,
        })
    findings.sort(key=lambda f: (-SEV_RANK.get(f["max_severity"], 1), f["name"]))
    return findings


async def resolve_all(deps: list[Dep], verify: bool = False) -> tuple[list[Dep], list[Dep]]:
    """Resolve versions concurrently. Returns (resolved, unknown_on_registry)."""
    uniq: dict[tuple, Dep] = {}
    for d in deps:
        uniq.setdefault((d.ecosystem, d.name, d.version, d.spec), d)
    sem = asyncio.Semaphore(8)

    async def _one(d: Dep):
        async with sem:
            return await resolve(d, verify)

    results = await asyncio.gather(*(_one(d) for d in list(uniq.values())[:MAX_DEPS]))
    resolved = [d for d, ok in results if ok and d.version]
    unknown = [d for d, ok in results if not ok]
    return resolved, unknown


async def scan_deps(deps: list[Dep], verify: bool = False) -> dict:
    """Full scan: resolve + OSV. Raises ScannerUnavailable."""
    resolved, unknown = await resolve_all(deps, verify)
    findings = await check(resolved)
    return {
        "checked": [asdict(d) for d in resolved],
        "unknown": [asdict(d) for d in unknown],
        "findings": findings,
        "verdict": "block" if findings else "go",
        "vulnerable": len(findings),
        "vuln_count": sum(f["count"] for f in findings),
    }
