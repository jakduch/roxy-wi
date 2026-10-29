import json
import pytest
from app.modules.db.db_model import InstallationTasks, OperationalAssessment, UserGroups, Server, PacemakerCluster
from app.modules.operations.queue import deserialize_operation_payload as unseal
from app.modules.service import proxy_assessments as assessments
from tests.product.test_proxy_diagnostics import diagnostic_host


def discover(client):
    response=client.post('/insights/api/services/discover',json=dict(service='haproxy',server_id=11))
    assert response.status_code==202,response.text
    assert assessments.execute(InstallationTasks.get_by_id(response.json['task_id']))=='completed'
    return response.json['id']


def confirm(client,ident,**values):
    return client.post('/insights/api/services/confirm',json=dict(assessment_id=ident,frontend_id='frontend:public',domain='example.test',note='Confirmed against the application registry',**values))


def test_discovery_is_durable_encrypted_and_confirmation_is_separate(product_client,diagnostic_host):
    ident=discover(product_client)
    assert OperationalAssessment.get_by_id(ident).result.startswith('fernet:')
    from app.modules.service import service_map
    service_map.view(1, OperationalAssessment.get_by_id(ident).user_id)
    row=product_client.get('/insights/api/services').json['data'][0]
    assert row['relationship']=='discovered' and row['domain']=='example.test'
    assert row['backends'][0]['host']=='192.0.2.88' and row['confirmation'] is None
    assert not diagnostic_host.calls # config/interface discovery only; no traffic probes
    response=confirm(product_client,ident);assert response.status_code==201,response.text
    row=product_client.get('/insights/api/services').json['data'][0]
    assert row['relationship']=='confirmed' and row['domain_origin']=='discovered'
    assert product_client.delete('/insights/api/services/confirm/'+str(response.json['id'])).status_code==200
    assert product_client.get('/insights/api/services').json['data'][0]['relationship']=='discovered'


def test_manual_domain_remains_operator_supplied_and_never_changes_configuration(product_client,diagnostic_host):
    ident=discover(product_client)
    response=product_client.post('/insights/api/services/confirm',json=dict(assessment_id=ident,frontend_id='frontend:public',domain='manual.example.test',note='Known application routing alias'))
    assert response.status_code==201
    rows=product_client.get('/insights/api/services').json['data']
    manual=next(row for row in rows if row['domain']=='manual.example.test')
    assert manual['domain_origin']=='operator-supplied' and manual['relationship']=='confirmed'
    assert not diagnostic_host.calls


def test_superseded_snapshot_and_unknown_frontend_are_rejected(product_client,diagnostic_host):
    ident=discover(product_client)
    assert product_client.post('/insights/api/services/confirm',json=dict(assessment_id=ident,frontend_id='shell:unknown',domain='example.test',note='Test unknown frontend')).status_code==400
    discover(product_client)
    assert confirm(product_client,ident).status_code==409
    assert product_client.get('/insights/api/assessments/'+str(ident)).status_code==200


def test_impact_links_registered_proxy_ha_and_backend_candidates_without_predicting_outage(product_client,product,diagnostic_host):
    Server.create(server_id=33,hostname='Backend',ip='192.0.2.88',group_id='1',cred_id=11)
    Server.create(server_id=44,hostname='HA peer',ip='192.0.2.44',group_id='1',cred_id=11)
    PacemakerCluster.create(name='Product HA',group_id=1,servers=json.dumps([dict(server_id=11,name='node1'),dict(server_id=44,name='node2')]))
    discover(product_client)
    for ident in (11,33,44):
        data=product_client.get('/insights/api/services?impact_node='+str(ident)).json
        assert len(data['data'])==1 and 'not an outage prediction' in data['summary']
    assert product_client.get('/insights/api/services?impact_node=22').status_code==400
    row=product_client.get('/insights/api/services').json['data'][0]
    assert row['ha'][0]['members']==[11,44]


def test_identity_change_is_visible_and_prevents_confirmation(product_client,diagnostic_host):
    ident=discover(product_client)
    Server.update(ip='192.0.2.77').where(Server.server_id==11).execute()
    assert not product_client.get('/insights/api/services').json['data'][0]['identity_current']
    assert confirm(product_client,ident).status_code==409


def test_tenant_and_read_only_role_cannot_confirm_or_probe(product_client,product,diagnostic_host):
    ident=discover(product_client)
    OperationalAssessment.update(group_id=2).where(OperationalAssessment.id==ident).execute()
    assert confirm(product_client,ident).status_code==404
    assert product_client.get('/insights/api/services').json['data']==[]
    UserGroups.update(user_role_id=4).where(UserGroups.user_id==product.admin.user_id).execute()
    assert confirm(product_client,ident).status_code==403
    assert product_client.post('/insights/api/services/discover',json=dict(service='haproxy',server_id=11)).status_code==403


def test_failed_refresh_retains_successful_snapshot_and_secret_paths_never_leave_parser(product_client,diagnostic_host,monkeypatch):
    ident=discover(product_client)
    monkeypatch.setattr(assessments.inventory,'discover',lambda *args:(_ for _ in ()).throw(RuntimeError('PRIVATE PASSWORD')))
    response=product_client.post('/insights/api/services/discover',json=dict(service='haproxy',server_id=11))
    assert assessments.execute(InstallationTasks.get_by_id(response.json['task_id']))=='failed'
    result=product_client.get('/insights/api/services')
    assert result.json['data'][0]['assessment_id']==ident
    assert 'PRIVATE PASSWORD' not in result.text and '/private/site.pem' not in result.text
