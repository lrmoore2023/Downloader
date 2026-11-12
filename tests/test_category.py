"""Creator Type (major · sub) derived from the destination folder."""
import pytest

from backend.api import Api


@pytest.mark.parametrize("dest, expected", [
    ("P:/Furry/2D/Someone", ("Furry", "2D")),
    ("P:/Hentai/3D/Someone", ("Hentai", "3D")),
    ("P:/Real/Creators/Someone", ("Real", "Real")),
    ("P:/Real/Furry/Someone", ("Real", "Furry")),
    # Diaper artists sit outside Major/Sub and file under their major as "Diaper".
    ("P:/Goon/Diaper/Artists/Furry/Someone", ("Furry", "Diaper")),
    ("P:/Goon/Diaper/Artists/Hentai/Someone", ("Hentai", "Diaper")),
    ("P:\\goon\\diaper\\artists\\hentai\\Someone\\", ("Hentai", "Diaper")),
    ("P:/Goon/Diaper/Artists/Other/Someone", ("Other", "")),
])
def test_derive_category_pair(dest, expected):
    assert Api._derive_category_pair(Api.__new__(Api), dest) == expected


def test_diaper_is_not_real():
    api = Api.__new__(Api)
    assert not api._is_real_dest("P:/Goon/Diaper/Artists/Hentai/Someone")
