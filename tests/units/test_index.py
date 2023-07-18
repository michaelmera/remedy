from assertpy import assert_that
from remedy.remarkable.filesource import FileSource
from remedy.remarkable.metadata import RemarkableIndex, ROOT_ID, TRASH_ID


class MemorySource(FileSource):
    def __init__(self) -> None:
        super().__init__()
        self.items = {}

    def listItems(self):
        yield from self.items


def test_index_has_root_folder() -> None:
    source = MemorySource()
    index = RemarkableIndex(source)

    assert_that(index.root()).is_not_none()
    assert_that(index.root().uid).is_equal_to(ROOT_ID)
    assert_that(index.root().typeName()).is_equal_to("folder")


def test_root_folder_has_no_parent() -> None:
    source = MemorySource()
    index = RemarkableIndex(source)

    assert_that(index.root()).is_not_none()
    assert_that(index.root().parentEntry()).is_none()


def test_index_has_trash_folder() -> None:
    source = MemorySource()
    index = RemarkableIndex(source)

    assert_that(index.trash).is_not_none()
    assert_that(index.trash.uid).is_equal_to(TRASH_ID)
    assert_that(index.trash.typeName()).is_equal_to("trash")


def test_trash_folder_has_no_parent() -> None:
    source = MemorySource()
    index = RemarkableIndex(source)

    assert_that(index.trash).is_not_none()
    assert_that(index.trash.parentEntry()).is_none()
