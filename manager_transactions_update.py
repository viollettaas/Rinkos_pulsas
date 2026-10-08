# -*- coding: utf-8 -*-
"""Automatinis CRIB vadovų sandorių papildymas.

Modulis paliktas atskiras, nes jį kviečia bendras programos mygtukas
„Atnaujinti duomenis“. Visa PDF nuskaitymo ir DB įrašymo logika yra
vadovu_sandoriai.py, kad tas pats kodas būtų naudojamas ir vadovų sandorių
puslapyje.
"""

from vadovu_sandoriai import update_manager_transactions_from_recent_crib

__all__ = ["update_manager_transactions_from_recent_crib"]
