// CAPTAIN: the one network the Weights page can open with nothing set up: starter:tiny, 2-3-1, 13 numbers. It
// answers "is exactly one of the two inputs on?". Its hidden neurons were set by hand, and one bias is set wrong
// on purpose, so "Both on" comes out wrong: a mistake a person or an agent can find and fix with one number.
// The digit reader is not kept here: Captain's services make one (and their own tiny network) the first time
// weights are opened in a workspace.
export const TINY = "starter:tiny";

export const STARTERS = [
  { id: TINY, name: "Tiny network", says: "2 inputs, 3 hidden neurons, 1 output. Every number fits on the screen." },
];

function tiny() {
  const input = (name, x, y) => ({ name, x, y: [y] });
  const examples = [input("Both off", [0, 0], 0), input("Only the first on", [1, 0], 1), input("Only the second on", [0, 1], 1), input("Both on", [1, 1], 0)];
  return {
    id: TINY, name: "Tiny network", sizes: [2, 3, 1], squish: "sigmoid", cost: "squared_error",
    layers: [
      // Hidden 1 turns on when either input is on, hidden 2 was meant to turn on only when both are, hidden 3
      // does little. Hidden 2's bias should be near -9: at -12 it stays half off when both inputs are on.
      { W: [[6, 6], [6, 6], [-2, 3]], b: [-3, -12, -1] },
      { W: [[7, -8, 0.4]], b: [-3] },
    ],
    inputNames: ["First input", "Second input"],
    outputNames: ["Exactly one is on"],
    examples,
    test: examples,
  };
}

/** A fresh copy of a starter network, or null when the id is not one. */
export function starter(id) {
  if (id === TINY) return tiny();
  return null;
}
