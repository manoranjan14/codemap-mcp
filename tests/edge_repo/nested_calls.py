def outer():
    helper_a()
    def inner():
        helper_b()
    inner()

def helper_a():
    pass

def helper_b():
    pass
