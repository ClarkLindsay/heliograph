from .goodreads import Goodreads
from .storygraph import StoryGraph
from .fable import Fable
from .pagebound import Pagebound
from .librarything import LibraryThing

# To add a service: write a Provider subclass (see providers/base.py), add its defaults to config.DEFAULTS,
# and list it here.
ALL = [Goodreads, StoryGraph, Fable, Pagebound, LibraryThing]


def build(config, log):
    return [cls(config, log) for cls in ALL]
