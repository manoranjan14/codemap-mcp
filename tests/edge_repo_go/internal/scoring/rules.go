package scoring

// Rules is a type with methods - the receiver makes instance-call
// resolution explicit in a way TypeScript needed inference for.
type Rules struct {
	Weight int
}

func (r *Rules) Apply(x int) int {
	return x * r.Weight
}

func (r Rules) Describe() string {
	return "rules"
}

// Compute is a package-level function, exported.
func Compute(x int) int {
	return x + 1
}

// helper is unexported - still a node, still callable within the package.
func helper() int {
	return 1
}

func UsesHelper() int {
	return helper()
}

// Function literals are everywhere in real Go - subtests, defer,
// goroutines, handler funcs - and a call inside one must not vanish.
func WithLiteral() {
	run(func() {
		helper()
	})
}

func run(f func()) { f() }

func WithDeferredLiteral() {
	defer func() {
		Compute(1)
	}()
}

func WithCallFreeLiteral() []int {
	return mapped(func(x int) int { return x + 1 })
}

func mapped(f func(int) int) []int { return nil }
