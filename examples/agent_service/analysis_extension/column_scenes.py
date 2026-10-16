"""Column comparisons and connected waterfalls from exact numeric spans."""
from decimal import Decimal, localcontext
from .models import AnalysisError, decimal_value


def column_scene(rows, kind, note, steps=None):
    if not rows: return None
    with localcontext() as context:
        context.prec = 512
        spans = steps if steps is not None else [(Decimal(0), decimal_value(r['exact'])) for r in rows]
        low = min(Decimal(0), *(min(a,b) for a,b in spans))
        high = max(Decimal(0), *(max(a,b) for a,b in spans))
        column = max(100, max(len(r['display'] + r['unit']) * 7 + 18 for r in rows))
        width = max(600, column * len(rows) + 48)
        if width > 10000: raise AnalysisError('DIAGRAM_LIMIT', '单图项目过多，请分组展示。')
        column = (width - 48) / len(rows)
        def y(n): return round(float(Decimal(280) - (n-low) / (high-low or Decimal(1)) * 220), 6)
        baseline = y(Decimal(0))
        primitives = [{'kind':'line','x1':24,'x2':width-24,'y1':baseline,'y2':baseline,'tone':'axis'}]
        lines_count = 1
        for i, (row, (start,end)) in enumerate(zip(rows,spans)):
            x = 24 + i * column
            a,b = y(start),y(end)
            tone = 'value' if row.get('total') else 'negative' if end < start else 'positive'
            primitives.append({'kind':'rect','x':x+column*.2,'y':min(a,b),'width':column*.6,'height':abs(a-b),'tone':tone,'rowKey':row['rowKey']})
            if start != end and abs(a-b) < 1:
                primitives.append({'kind':'line','x1':x+column*.2,'x2':x+column*.8,'y1':b,'y2':b,'tone':tone})
            if steps is not None and i + 1 < len(rows) and not rows[i+1].get('total'):
                primitives.append({'kind':'line','x1':x+column*.8,'x2':x+column*1.2,'y1':b,'y2':b,'tone':'axis'})
            primitives.append({'kind':'text','x':x+8,'y':max(20,min(a,b)-10),'text':row['display']+(' '+row['unit'] if row['unit'] else ''),'tone':'value'})
            count = max(4,int((column-16)/13))
            lines = [row['label'][j:j+count] for j in range(0,len(row['label']),count)] or ['']
            lines_count=max(lines_count,len(lines))
            primitives.extend({'kind':'text','x':x+8,'y':310+j*18,'text':part,'tone':'label'} for j,part in enumerate(lines))
        if len(primitives)>1000: raise AnalysisError('DIAGRAM_LIMIT','图表超过展示容量。')
        return {'type':kind,'width':width,'height':330+lines_count*18,'zeroBaseline':baseline,
                'description':note,'rowKeys':[r['rowKey'] for r in rows],'primitives':primitives}
