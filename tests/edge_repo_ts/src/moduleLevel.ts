// Fixture for top-level call attribution in TS. In a composition-API
// codebase most composable wiring happens here, at module scope, not
// inside a function - so dropping these calls drops the majority of the
// "who uses this composable" signal.
import { helperA } from "./utils/helpers"

export function tsTopTarget() {
    return 1
}

function nestedOnlyTarget() {
    return 2
}

// All three are module-scope calls.
const direct = tsTopTarget()
const { destructured } = helperA()
helperA()

export function tsWrapper() {
    // Belongs to tsWrapper, not to the module.
    return nestedOnlyTarget()
}

// A named arrow's body is its own scope, not the module's.
const arrowOwner = () => nestedOnlyTarget()
