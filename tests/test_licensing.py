"""Лицензия: подпись Ed25519 (RFC 8032), ключи, 30 дней пробного периода и что без ключа не работает."""
import datetime as dt
from argparse import Namespace

import pytest

from filecleaner import cli, ed25519, licensing

RFC8032 = [  # раздел 7.1: закрытый ключ, открытый, сообщение, подпись
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
     "d25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc659"
     "4a7c15e9716ed28dc027beceea1ec40a"),
]
SELLER = bytes(range(32))  # тестовый закрытый ключ продавца


@pytest.fixture
def seller(sandbox, monkeypatch):
    monkeypatch.setattr(licensing, "PUBLIC_KEY", ed25519.public_key(SELLER))
    return SELLER


def expire() -> None:
    licensing._save({"trial_started": (dt.date.today() - dt.timedelta(days=31)).isoformat()})


def test_ed25519_rfc8032_vectors():
    for secret, public, message, signature in RFC8032:
        secret, public, message, signature = map(bytes.fromhex, (secret, public, message, signature))
        assert ed25519.public_key(secret) == public
        assert ed25519.sign(secret, message) == signature
        assert ed25519.verify(public, message, signature)
        assert not ed25519.verify(public, message + b"!", signature)


def test_key_round_trip_and_forgery(seller):
    key = licensing.issue(seller, "Ярослав  Б.", issued=dt.date(2026, 9, 26))
    found = licensing.parse("  " + key.lower().replace("-", " ") + "\n")   # регистр, пробелы и переносы не важны
    assert (found.name, found.issued, found.key) == ("Ярослав Б.", dt.date(2026, 9, 26), key)
    forged = key[:10] + ("A" if key[10] != "A" else "B") + key[11:]
    for bad in (forged, key[:-5], "ABC-" + key, "FC1-HELLO", ""):
        with pytest.raises(licensing.LicenseError):
            licensing.parse(bad)
    with pytest.raises(licensing.LicenseError):
        licensing.parse(licensing.issue(bytes(32), "Чужой продавец"))       # подписан не нашим ключом


def test_trial_then_key(seller):
    first = dt.date(2026, 9, 1)
    assert licensing.status(first)["days_left"] == 30                      # первый запуск начинает пробный период
    assert licensing.status(first + dt.timedelta(days=29)) == {
        "state": "trial", "name": None, "days_left": 1, "buy_url": licensing.BUY_URL}
    assert licensing.status(first + dt.timedelta(days=30))["state"] == "expired"

    expire()
    with pytest.raises(licensing.LicenseError):
        licensing.require()
    with pytest.raises(licensing.LicenseError):
        licensing.activate("FC1-AAAAA-BBBBB")
    info = licensing.activate(licensing.issue(seller, "Покупатель"))
    assert info == {"state": "licensed", "name": "Покупатель", "days_left": None, "buy_url": licensing.BUY_URL}
    licensing.require()                                                      # с ключом — можно


def test_without_key_only_preview(seller, sandbox, rules, capsys, monkeypatch):
    from filecleaner import refs

    monkeypatch.setattr(refs, "scan_references", lambda *a, **k: {})  # не обходить настоящую систему
    expire()
    lab = sandbox / "Downloads" / "lab1.pdf"
    lab.write_bytes(b"x" * 2000)
    assert cli.cmd_sort(Namespace(folder=str(lab.parent), apply=False, yes=False), rules) == 0   # смотреть можно
    assert cli.cmd_sort(Namespace(folder=str(lab.parent), apply=True, yes=True), rules) == 3
    assert lab.exists()                                                      # ничего не тронуто
    assert "Пробный период закончился" in capsys.readouterr().out
