package scoring

// A second file in the SAME package. A `scoring.Sibling()` call from
// another package must resolve here, which is why package lookup cannot be
// keyed on a single file the way Python and TS imports are.
func Sibling() int {
	return Compute(2)
}
