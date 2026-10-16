from pathlib import Path
import tempfile
from unittest import IsolatedAsyncioTestCase

from analysis_fixtures import OWNER, saved_fixture, rules
from analysis_extension.delivery import AnalysisDelivery
from analysis_extension.service import AnalysisService
from analysis_extension.store import AnalysisStore


class PresentationCase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AnalysisStore(self.root / "analysis-data")
        self.rules = rules()
        self.delivery = AnalysisDelivery(self.store, OWNER, self.root / "workspace", self.rules)
        async def authorize():
            pass
        self.service = AnalysisService(self.store, OWNER, authorize, self.rules, delivery=self.delivery)

    async def publish(self, values=("100", "-1033.14", "-8300"), unit="元"):
        ref, selectors, scope = saved_fixture(self.store, values, unit=unit)
        result = await self.service.build([ref], selectors, scope, [
            {"id": "order", "operator": "SORT"}, {"id": "rank", "operator": "RANK"}, {"id": "count", "operator": "COUNT"},
            {"id": "sum", "operator": "SUM", "ruleRef": "fixture-additive"}], "build")
        self.ref = result["analysisRef"]
        views = [{"viewId": "view-" + v["derivedId"], "scopeId": result["scope"]["scopeId"],
                  "type": "BAR_RANK" if v["operator"] == "RANK" else "METRIC", "valueRefs": [v["derivedId"]]}
                 for v in result["derivedValues"] if v["operator"] in ("RANK", "COUNT", "SUM") and v["calculationValidation"] == "PASS"]
        expanded = []
        for view in views:
            if view['valueRefs'] == ['derived_count']:
                expanded.extend({**view, 'viewId': view['viewId'] + '-' + field, 'field': field}
                                for field in ('positive', 'negative', 'zero', 'unavailable'))
            else:
                expanded.append(view)
        views = expanded + [{'viewId':'display-order','scopeId':result['scope']['scopeId'],'type':'ORDER','valueRefs':['derived_order']}]
        artifact = self.store.get_analysis(OWNER, self.ref)
        views.append({'viewId': 'details', 'scopeId': result['scope']['scopeId'], 'type': 'TABLE', 'valueRefs': [f['factId'] for f in artifact['facts']]})
        views = [{**view, 'showUnit': True} for view in views]
        published = await self.service.publish(self.ref, [], views, "publish", 0)
        self.path = published["delivery"]["path"]
        self.artifact = self.store.get_analysis(OWNER, self.ref)
        return published
