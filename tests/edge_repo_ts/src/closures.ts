// Fixture for anonymous callback / closure tracking (Task #16 part B)

function realWork() {
  return 42;
}

function otherWork() {
  return 99;
}

// 1. Call-bearing anonymous arrow callback (block body) -> should get its own closure node
export function useEffectLike(cb: () => void) {
  cb();
}

export function withArrowBlockCallback() {
  useEffectLike(() => {
    realWork();
  });
}

// 2. Call-bearing anonymous function(...) expression callback -> should get its own closure node
export function withFunctionExprCallback() {
  useEffectLike(function () {
    otherWork();
  });
}

// 3. Trivial call-free callback -> should be SKIPPED (no closure node, no graph bloat)
export function withTrivialCallback() {
  const ids = [1, 2, 3].map(x => x + 1);
  return ids;
}

// 4. Nested closure-within-closure
export function withNestedClosure() {
  useEffectLike(() => {
    useEffectLike(() => {
      realWork();
    });
  });
}

// 5. Concise-body NAMED arrow function -> regression check for _direct_calls/scope_node fix
const validate = (x: number) => realWork();

export function useValidate() {
  return validate(5);
}
