"""helpers.label_supports — is the product name the model returned in the label
text it transcribed first? Pure: no database, no network.

The model is asked to write down what it can read BEFORE naming the product. A
name word that isn't in its own reading is a name from memory: the Gatorade whose
label said "Blue Bolt" coming back as "Glacier Freeze". The check has to refuse
that while accepting everything that is merely formatting — a refused good read
costs the bartender a retake.
"""
import pytest

from helpers import _one_edit_apart, label_supports


@pytest.mark.parametrize("name, brand, label", [
    ("Blue Bolt", "Gatorade", "GATORADE THIRST QUENCHER BLUE BOLT 28 FL OZ"),
    ("Old No. 7", "Jack Daniel's", "Jack Daniel's Old No.7 Brand Tennessee Sour Mash Whiskey"),
    ("Red Label", "Johnnie Walker", "JOHNNIE WALKER RED LABEL BLENDED SCOTCH WHISKY"),
    ("Añejo", "Patrón", "PATRON ANEJO TEQUILA"),                   # accents
    ("Citron", "Ketel One", "KETEL ONE CITROEN"),                  # one letter off
    ("VSOP", "Hennessy", "HENNESSY V.S.O.P PRIVILEGE COGNAC"),     # punctuation inside a word
    ("12", "Glenfiddich", "GLENFIDDICH AGED 12 YEARS SINGLE MALT"),
    ("Jack Daniel's Old No. 7", "Jack Daniel's", "JACK DANIEL'S OLD NO. 7"),  # brand repeated
    ("Handmade 750ml", "Tito's", "TITO'S HANDMADE VODKA"),         # size in the name
    ("Original", "Grey Goose", "GREY GOOSE VODKA IMPORTED FROM FRANCE"),  # base product
    ("Grey Goose", "Grey Goose", "GREY GOOSE VODKA"),              # name is only the brand
    ("Le Citron", "Grey Goose", "GREY GOOSE LE CITRON"),
])
def test_accepts_names_that_are_on_the_label(name, brand, label):
    assert label_supports(name, brand, label) is True


@pytest.mark.parametrize("name, brand, label", [
    ("Glacier Freeze", "Gatorade", "GATORADE THIRST QUENCHER BLUE BOLT 28 FL OZ"),
    ("Black Label", "Johnnie Walker", "JOHNNIE WALKER RED LABEL BLENDED SCOTCH WHISKY"),
    ("15", "Glenfiddich", "GLENFIDDICH AGED 12 YEARS SINGLE MALT"),
    ("Mandrin", "Absolut", "ABSOLUT CITRON VODKA"),
    # the variant isn't in the model's own reading at all
    ("Old No. 7", "Jack Daniel's", "Jack Daniel's Tennessee Whiskey 750ml"),
    # a short word must be a whole word: "7" is not in "750"
    ("No 7", "Jack Daniel's", "JACK DANIEL'S 750 ML"),
])
def test_refuses_names_that_are_not(name, brand, label):
    assert label_supports(name, brand, label) is False


@pytest.mark.parametrize("label", ["", None, "   ", "—"])
def test_no_label_text_is_no_verdict(label):
    assert label_supports("Blue Bolt", "Gatorade", label) is None


def test_one_edit_apart():
    assert _one_edit_apart("citron", "citroen")
    assert _one_edit_apart("citroen", "citron")
    assert _one_edit_apart("mandrin", "mandrim")
    assert not _one_edit_apart("mandrin", "citron")
    assert not _one_edit_apart("reposado", "resposadoo")
