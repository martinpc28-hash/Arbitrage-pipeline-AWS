"""
Convierte la respuesta de GET /v4/odds de OddsPapi en mercados agrupados,
con la mejor cuota disponible por resultado entre todas las casas.

CONFIRMADO CONTRA LA API REAL: no hay un campo "line" separado. Cada linea
distinta (ej. "mas de 2.5" vs "mas de 3.5") es un market_id numerico
diferente, asi que agrupar por market_id ya es seguro y no mezcla lineas.
"""

from typing import Optional
from common.arbitrage import OutcomePrice

# Casas excluidas a pedido del usuario (ej. no tiene cuenta ahi / no confia en ella).
# Se ignoran por completo, en todos los mercados.
BOOKMAKERS_EXCLUIDOS = {"betfair-ex"}


def _line_de_mercado(market_obj):
    return market_obj.get("line")


def _es_precio_activo(player_obj):
    return bool(player_obj.get("active", True)) and player_obj.get("price") is not None


def agrupar_mejores_cuotas_por_mercado(odds_response):
    mejor = {}
    etiquetas = {}
    for bm_name, bm_data in odds_response.get("bookmakerOdds", {}).items():
        if bm_name in BOOKMAKERS_EXCLUIDOS:
            continue
        if not bm_data.get("bookmakerIsActive", True):
            continue
        # Link al evento en esta casa (se usa como fallback si no hay betslip especifico)
        fixture_link = bm_data.get("fixturePath")
        for market_id, market_obj in bm_data.get("markets", {}).items():
            line = _line_de_mercado(market_obj)
            market_key = str(market_id) + "::" + (str(line) if line is not None else "")
            etiquetas.setdefault(market_key, market_obj.get("marketName"))
            for outcome_id, outcome_obj in market_obj.get("outcomes", {}).items():
                for player_id, player_obj in outcome_obj.get("players", {}).items():
                    if not _es_precio_activo(player_obj):
                        continue
                    rkey = outcome_id if player_id == "0" else (str(outcome_id) + ":" + str(player_id))
                    precio = float(player_obj["price"])
                    # Preferimos el deep-link exacto a la apuesta (betslip) si existe,
                    # si no, el link general al evento en esa casa.
                    link = player_obj.get("betslip") or fixture_link
                    cand = OutcomePrice(
                        outcome_id=rkey,
                        outcome_label=outcome_obj.get("outcomeLabel") or player_obj.get("playerName"),
                        price=precio, bookmaker=bm_name, link=link,
                    )
                    rm = mejor.setdefault(market_key, {})
                    actual = rm.get(rkey)
                    if actual is None or precio > actual.price:
                        rm[rkey] = cand
    return ({k: list(v.values()) for k, v in mejor.items()}, etiquetas)
