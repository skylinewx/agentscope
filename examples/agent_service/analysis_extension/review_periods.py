"""A small Chinese period legend for complete model evidence; raw inputs stay exact."""
from .business_tables import period_label


def period_labels(material):
    labels = {}
    def visit(value, key=None):
        if key in ('code', 'html', 'documentText', 'documentSourceText'):
            return
        if isinstance(value, str):
            label = period_label(value)
            if label != value:
                labels[value] = label
        elif isinstance(value, dict):
            for name, child in value.items():
                visit(child, name)
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
    visit(material)
    if not labels:
        return None
    return {'names': dict(sorted(labels.items())),
            'meaning': '时期编码对应的中文名称。原始输入和代码仍使用编码；按此对照理解，不把编码当普通数值或忽略季度。'
                       '用户请求的时期、数据加载时期和实际数据时期可能不同。按每条输入的 period、dataPeriod 或 dimensions.DATATIME 核对实际覆盖范围及分组。'
                       '按年度加载的数据可能包含 12 个月或 4 个季度。'
                       '累计等范围仍由查询范围模式确定；中文名称只解释单期编码，不改变累计截止期或筛选条件。'}
