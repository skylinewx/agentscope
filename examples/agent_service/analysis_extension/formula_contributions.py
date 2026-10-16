"""Optional exact linear contribution metadata, without business attribution."""
from decimal import Decimal, localcontext, Inexact, DecimalException
from .comparison_scenes import affine
from .formulas import Expression
from .models import AnalysisError, decimal_value
from .formatting import number_text


def contributions(node, facts, view):
    try:
        with localcontext() as context:
            context.prec=512
            context.traps[Inexact]=True
            coefficients={}
            for address,c in affine(Expression(node['expression']).tree):
                if address is not None: coefficients[address]=coefficients.get(address,Decimal(0))+c
            result={}
            for dep in node['dependencies']:
                fact=facts.get(dep.get('factRef'))
                if not fact or fact.get('valueState')!='PRESENT': continue
                coefficient=coefficients.get(dep['address'],Decimal(0))
                delta=coefficient*decimal_value(fact['value'])
                result[dep['factRef']]={'operator':'−' if coefficient<0 else '+',
                    'coefficient':str(abs(coefficient)) if abs(coefficient)!=1 else '',
                    'contribution':number_text(str(delta),view.get('numberFormat'),grouping=True),
                    'tone':'negative' if delta<0 else 'positive','delta':delta}
            maximum=max((abs(r['delta']) for r in result.values()),default=Decimal(0))
            context.traps[Inexact]=False  # Only geometric percentages may round.
            for item in result.values():
                item['weight']=round(float(abs(item.pop('delta'))/(maximum or Decimal(1))*100),6)
            return result
    except (AnalysisError,DecimalException):
        return {}  # Nonlinear expressions retain their exact equation; no fake additive attribution.
