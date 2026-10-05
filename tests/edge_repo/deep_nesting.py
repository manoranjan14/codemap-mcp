class A:
    class B:
        class C:
            def method(self):
                def local_fn():
                    def deeper():
                        pass
                    deeper()
                local_fn()
