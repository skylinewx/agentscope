"""Scene selectors used by tests of the current flat scene contract."""
def comparison(document):
    return next((s for s in document['scenes'] if s['type'] in ('BAR_VALUES', 'BAR_RANK')), None)


def diagrams(document):
    return [s for s in document['scenes'] if s['type'] not in ('BAR_VALUES', 'BAR_RANK')]
