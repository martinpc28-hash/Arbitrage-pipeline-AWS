"""
Logica de deteccion de arbitraje (surebets) entre casas de apuestas.
Sin dependencias de AWS, testeable de forma aislada.

Formula general (2, 3 o N resultados):
    implicacion_total = suma( 1 / mejor_cuota(resultado_i) )
    Si implicacion_total < 1  ->  hay arbitraje.
    apuesta(resultado_i) = (1 / mejor_cuota(resultado_i)) / implicacion_total * presupuesto
    ganancia = presupuesto / implicacion_total - presupuesto
"""

from dataclasses import dataclass
from typing import Optional

PRESUPUESTO_BASE = 1000.0


@dataclass
class OutcomePrice:
    outcome_id: str
    outcome_label: Optional[str]
    price: float
    bookmaker: str
    link: Optional[str] = None  # link al evento en la casa de apuestas (fixturePath o betslip)


@dataclass
class ArbitrageOpportunity:
    market_key: str
    market_label: Optional[str]
    outcomes: list
    implied_probability_sum: float
    profit_pct: float
    stakes: dict

    def to_dict(self) -> dict:
        return {
            "marketKey": self.market_key,
            "marketLabel": self.market_label,
            "impliedProbabilitySum": round(self.implied_probability_sum, 5),
            "profitPct": round(self.profit_pct, 3),
            "isArbitrage": self.implied_probability_sum < 1.0,
            "budget": PRESUPUESTO_BASE,
            "legs": [
                {
                    "outcomeId": o.outcome_id,
                    "outcomeLabel": o.outcome_label,
                    "bookmaker": o.bookmaker,
                    "price": o.price,
                    "stake": round(self.stakes[o.outcome_id], 2),
                    "payout": round(self.stakes[o.outcome_id] * o.price, 2),
                    "link": o.link,
                }
                for o in self.outcomes
            ],
        }


def _evaluar(market_key, market_label, mejores_cuotas):
    if len(mejores_cuotas) < 2:
        return None
    implicacion_total = sum(1.0 / o.price for o in mejores_cuotas)
    stakes = {o.outcome_id: (1.0 / o.price) / implicacion_total * PRESUPUESTO_BASE for o in mejores_cuotas}
    profit_pct = (PRESUPUESTO_BASE / implicacion_total - PRESUPUESTO_BASE) / PRESUPUESTO_BASE * 100
    return ArbitrageOpportunity(
        market_key=market_key, market_label=market_label, outcomes=mejores_cuotas,
        implied_probability_sum=implicacion_total, profit_pct=profit_pct, stakes=stakes,
    )


def evaluar_mercado(market_key, market_label, mejores_cuotas):
    """Solo devuelve resultado si HAY arbitraje (implicacion < 1)."""
    op = _evaluar(market_key, market_label, mejores_cuotas)
    if op is None or op.implied_probability_sum >= 1.0:
        return None
    return op


def evaluar_mercado_siempre(market_key, market_label, mejores_cuotas):
    """Devuelve el resultado del calculo SIEMPRE, haya o no arbitraje, para
    poder mostrar el razonamiento matematico de cada mercado escaneado."""
    return _evaluar(market_key, market_label, mejores_cuotas)
