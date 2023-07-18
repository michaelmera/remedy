from assertpy import assert_that
from remedy.remarkable.filesource import FileSource
from remedy.remarkable.metadata import RemarkableIndex


class MemorySource(FileSource):
    def listItems(self):
        return {}


def test_index_has_root_folder() -> None:
    source = MemorySource()
    index = RemarkableIndex(source)

    assert_that(index.root()).is_not_none()
