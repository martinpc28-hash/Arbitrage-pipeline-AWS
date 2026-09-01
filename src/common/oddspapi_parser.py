"""
Convierte la respuesta de GET /v4/odds de OddsPapi en mercados agrupados,
con la mejor cuota disponible por resultado entre todas las casas.

CONFIRMADO CONTRA LA API REAL: no hay un campo "line" separado. Cada linea
distinta (ej. "mas de 2.5" vs "mas de 3.5") es un market_id numerico
diferente, asi que agrupar por market_id ya es seguro y no mezcla lineas.
"""

import statistics
from typing import Optional
from common.arbitrage import OutcomePrice

# Casas excluidas a pedido del usuario (ej. no tiene cuenta ahi / no confia en ella).
# Se ignoran por completo, en todos los mercados.
BOOKMAKERS_EXCLUIDOS = {"betfair-ex"}

# Deteccion de outliers por dato de cuota erroneo/desactualizado, sin importar
# de que casa venga (visto en produccion: balkanbet.rs y gamdom devolviendo
# 9.6-10.0 en un mercado donde el resto de casas cotizaba ~2.5). Con al menos
# esta cantidad de casas cotizando el mismo resultado, se calcula la mediana
# de todas; cualquier cuota que la supere por mas de este multiplicador se
# descarta como sospechosa al elegir "la mejor". Con menos casas no hay base
# estadistica suficiente para juzgar, asi que se usa la mas alta tal cual.
MIN_CASAS_PARA_DETECTAR_OUTLIER = 3
MULTIPLICADOR_OUTLIER = 2.0


def _line_de_mercado(market_obj):
    return market_obj.get("line")


def _es_precio_activo(player_obj):
    return bool(player_obj.get("active", True)) and player_obj.get("price") is not None


def _mejor_precio_sin_outliers(candidatos):
    """De todas las cuotas de un mismo resultado (entre casas), elige la mas
    alta que no sea un outlier estadistico frente a las demas."""
    if len(candidatos) < MIN_CASAS_PARA_DETECTAR_OUTLIER:
        return max(candidatos, key=lambda c: c.price)
    mediana = statistics.median(c.price for c in candidatos)
    validos = [c for c in candidatos if c.price <= mediana * MULTIPLICADOR_OUTLIER]
    if not validos:
        # Si TODAS las cuotas son muy dispares entre si, no hay una base
        # confiable para descartar ninguna en particular: mejor no perder el
        # mercado por completo que arriesgarse a descartar la buena.
        validos = candidatos
    return max(validos, key=lambda c: c.price)


def agrupar_mejores_cuotas_por_mercado(odds_response):
    candidatos_por_resultado = {}
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
                    candidatos_por_resultado.setdefault(market_key, {}).setdefault(rkey, []).append(cand)

    mejor = {
        market_key: {
            rkey: _mejor_precio_sin_outliers(candidatos)
            for rkey, candidatos in resultados.items()
        }
        for market_key, resultados in candidatos_por_resultado.items()
    }
    return ({k: list(v.values()) for k, v in mejor.items()}, etiquetas)
