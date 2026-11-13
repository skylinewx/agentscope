import re

def visible_markup(value):
    """Ignore host identity comments when asserting unchanged visible HTML."""
    return re.sub(r'<!--analysis-static:.*?-->', '', value)
