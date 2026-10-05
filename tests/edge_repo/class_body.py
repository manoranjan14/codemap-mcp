"""Fixture for calls made in a CLASS BODY and in DECORATORS.

Both run at definition time and are real calls, but neither was ever
attributed to anything: _direct_calls skips ClassDef outright, and
_visit_def only collected calls for functions/methods. This is the shape
Django/SQLAlchemy/Pydantic code is built from - the calls are in the class
body, not in a method.
"""


def field(**kwargs):
    return kwargs


def validator(name):
    def wrap(fn):
        return fn
    return wrap


def register(cls):
    return cls


def module_level_only():
    return 1


@register
class Model:
    # Attributed to the CLASS node - this is where it runs.
    col = field(primary=True)
    other = field()

    @validator("col")
    def check(self):
        # Attributed to `check`, not to Model - the existing scope rule.
        return module_level_only()


class Plain:
    pass
