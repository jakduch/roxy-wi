import os
from pathlib import Path
import pytest
from playwright.sync_api import expect
from app.modules.db.db_model import InstallationTasks
from app.modules.service import proxy_assessments
from tests.product.test_proxy_diagnostics import diagnostic_host

pytestmark=[pytest.mark.browser,pytest.mark.skipif(os.environ.get('ROXYWI_TEST_BROWSER')!='1',reason='Requires Chromium')]


def test_publication_map_discovery_confirmation_and_impact_use_native_controls(logged_in,product_url,diagnostic_host):
    page=logged_in;page.goto(product_url+'/insights/services')
    page.locator('#map-server-button').click();page.get_by_role('option',name='Product HAProxy (192.0.2.11)',exact=True).click()
    with page.expect_response('**/services/discover') as queued:page.get_by_role('button',name='Discover proxy configuration').click()
    assert queued.value.status==202
    assert proxy_assessments.execute(InstallationTasks.get_by_id(queued.value.json()['task_id']))=='completed'
    expect(page.locator('#map-rows')).to_contain_text('example.test',timeout=10000)
    page.get_by_role('button',name='Confirm relationship',exact=True).click()
    page.locator('#map-note').fill('Confirmed against application inventory')
    page.get_by_role('button',name='Record confirmation',exact=True).click()
    expect(page.locator('#map-rows')).to_contain_text('confirmed · discovered')
    if os.environ.get('ROXYWI_CAPTURE_MAP_DOCS')=='1':
        page.wait_for_load_state('networkidle');folder=Path(__file__).resolve().parents[3]/'docs/screenshots/service-map';folder.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(folder/'map.png'))
    page.set_viewport_size(dict(width=390,height=844));page.wait_for_function("getComputedStyle(document.querySelector('.container')).marginLeft === '0px'")
    assert page.locator('main').evaluate('(node)=>node.scrollWidth<=node.clientWidth+1')
    page.get_by_role('button',name='Retract confirmation',exact=True).click()
    expect(page.locator('#map-rows')).to_contain_text('discovered · discovered')
