"""Fixture for self/instance-method resolution via lightweight type inference
(Task #17). Exercises: same-file ambiguous method names (two unrelated
classes each defining a method called `run`), same-file inheritance
(a method only on the base, same file), cross-file inheritance (base class
imported from repo_base.py), constructor-assigned attribute types
(self.x = Class()), a bare type annotation with no assignment
(self.x: Class), and a local variable instantiation."""

from repo_base import RemoteBase
from repo_helper import Helper


class Unrelated:
    def run(self):
        return "unrelated"


class Worker:
    def run(self):
        return "worker"

    def dispatch(self):
        # Same-file ambiguity: both Unrelated.run and Worker.run exist by
        # simple name in this file - the OLD by-name heuristic would see 2
        # same-file candidates and mark this external. With type inference,
        # we know `self` here is a Worker, so this must resolve to
        # Worker.run specifically, not Unrelated.run and not "ambiguous".
        return self.run()


class BaseThing:
    def shared(self):
        return "shared"


class Derived(BaseThing):
    def use_inherited(self):
        # Not defined on Derived itself - only reachable via BaseThing,
        # same file. Old heuristic already handled this by luck (only one
        # same-file `shared`); this confirms the MRO path gets it too.
        return self.shared()


class RemoteUser(RemoteBase):
    def use_remote(self):
        # RemoteBase.remote_method is defined in ANOTHER file - only
        # resolvable via cross-file inheritance (import-bound base class).
        return self.remote_method()


class Composed:
    def __init__(self):
        self.helper = Helper()
        self.untyped_ok: Helper

    def do_work(self):
        # self.helper's type was inferred from the constructor assignment
        # above - this should resolve to Helper.assist, not fall to
        # external just because Composed itself has no `assist` method.
        return self.helper.assist()

    def do_local(self):
        local = Helper()
        return local.assist()

    def do_unknown_attr(self):
        # self.other was never assigned/annotated anywhere - type unknown,
        # must stay external/unresolved, not guess.
        return self.other.assist()
