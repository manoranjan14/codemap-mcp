// Fixture for calls in a TS class body: property initialisers and
// parameter-property defaults run at construction/definition time but were
// never attributed to anything.
export function makeField(): number {
    return 1
}

export function makeOther(): number {
    return 2
}

function usedOnlyInAMethod(): number {
    return 3
}

export class Model {
    // Attributed to the CLASS node.
    col = makeField()
    other: number = makeOther()

    run() {
        // Attributed to `run`, not to Model.
        return usedOnlyInAMethod()
    }
}
