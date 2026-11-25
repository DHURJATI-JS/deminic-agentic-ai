// Coding section: Editor, Scanner, Stress test, GitHub tabs + chat cards +
// "Apply to workspace" buttons on AI code blocks (Chat & Memory tabs come from the shell).
import { installApplyEnhancer } from './coding/apply.js';
import { registerCodingCards } from './coding/cards.js';
import { editorTab } from './coding/editor.js';
import { githubTab } from './coding/github.js';
import { scannerTab } from './coding/scanner.js';
import { stressTab } from './coding/stress.js';

export const tabs = [editorTab, scannerTab, stressTab, githubTab];

export function registerCards(registerCard) {
  registerCodingCards(registerCard);
}

let installed = false;
export function init() {
  if (installed) return;
  installed = true;
  installApplyEnhancer();
}
