// Fixture for self/instance-method resolution via lightweight type
// inference (Task #17), TS/JS side. Mirrors
// tests/edge_repo/self_resolution.py's cases: same-file ambiguous method
// names, same-file and cross-file inheritance (`extends`), a field type
// annotation, a constructor parameter property, a `this.x = new X()`
// assignment, and a local variable instantiation.

import { RemoteBase } from "./remoteBase";
import { Helper } from "./helper";

class Unrelated {
  run() {
    return "unrelated";
  }
}

class Worker {
  run() {
    return "worker";
  }

  dispatch() {
    // Same-file ambiguity: Unrelated.run and Worker.run both exist by
    // simple name in this file - must resolve to Worker.run specifically.
    return this.run();
  }
}

class BaseThing {
  shared() {
    return "shared";
  }
}

class Derived extends BaseThing {
  useInherited() {
    return this.shared();
  }
}

class RemoteUser extends RemoteBase {
  useRemote() {
    // remoteMethod is only defined on RemoteBase, in another file - only
    // resolvable via cross-file `extends` + import binding.
    return this.remoteMethod();
  }
}

class ComposedField {
  private helper: Helper; // field type annotation, no assignment needed

  useField() {
    return this.helper.assist();
  }
}

class ComposedParamProp {
  constructor(private helper: Helper) {} // constructor parameter property

  useParamProp() {
    return this.helper.assist();
  }
}

class ComposedAssign {
  doWork() {
    this.helper = new Helper();
    return this.helper.assist();
  }

  doLocal() {
    const local = new Helper();
    return local.assist();
  }

  doUnknownAttr() {
    // this.other was never typed/assigned anywhere - must stay
    // external/unresolved, not guess.
    return this.other.assist();
  }
}
