import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import pytest
from app.modules.common.time import utc_now, utc_iso
from app.modules.db.db_model import (PacemakerCluster, Server, InstallationTasks, HaReadinessProfile,
    OperationalAssessment, UserGroups, HaCluster, HaClusterRouter, HaClusterSlave, HaClusterVip)
from app.modules.operations.queue import deserialize_operation_payload
from app.modules.service import ha_readiness as audit


@pytest.mark.parametrize('active,expected', [
    ('Yes (client certificate sent)', True), ('Yes', True),
    ('No', False), ('Yesterday', False), ('', False),
])
def test_qdevice_tls_observation_accepts_live_certificate_annotation(monkeypatch, active, expected):
    # Debian 13 and Rocky 9 append the client-certificate detail to TLS active.
    client = ('QNetd host:\t\t192.0.2.99:5403\nTLS:\t\t\tRequired\n'
              'State:\t\t\tConnected\nTLS active:\t\t' + active + '\nConnected since:\t2026-10-29T01:13:29\n')
    def run(argv, **_kwargs):
        return {'corosync-cmapctl': 'totem.cluster_name (str) = product-ha',
                'cibadmin': '<cib><configuration/></cib>', 'python3': 'a' * 64,
                'crm_mon': 'state', 'corosync-quorumtool': 'quorum',
                'corosync-qdevice-tool': client}[argv[2] if argv[0] == 'env' else argv[0]]
    monkeypatch.setattr(audit, 'Remote', lambda _server: SimpleNamespace(run=run))
    monkeypatch.setattr(audit, 'summary', lambda _: dict(quorate=True))
    monkeypatch.setattr(audit, 'details', lambda _: dict(configured=True, votes=1))
    node = audit.pacemaker_node(SimpleNamespace(name='product-ha'), 'one', SimpleNamespace(server_id=11))
    assert node['witness'] == dict(tls=expected, host='192.0.2.99:5403')


@pytest.fixture
def readiness_cluster(product,monkeypatch):
    second=Server.create(server_id=33,hostname='Product standby',ip='192.0.2.33',group_id='1',cred_id=11,haproxy=1)
    cluster=PacemakerCluster.create(name='product-ha',group_id=1,servers=json.dumps([
        dict(server_id=11,name='one',address='192.0.2.11',nodeid=1),dict(server_id=33,name='two',address='192.0.2.33',nodeid=2)]))
    xml='<cib><configuration><crm_config/><resources><primitive id="fence" class="stonith" type="fence_pve"><instance_attributes id="p"><nvpair name="pcmk_host_list" value="one two"/><nvpair name="passwd" value="NeverExposeThisSecret"/></instance_attributes></primitive></resources></configuration></cib>'
    observations={name:dict(id=ident,name=name,digest='a'*64,corosync_digest='b'*64,
        state=dict(quorate=True,nodes=[dict(name=n,online='true',unclean='false') for n in ('one','two')],failures=0,resources=[]),
        quorum=dict(configured=True,votes=1,two_node=False),witness=dict(tls=True,host='192.0.2.99:5403'),_xml=xml)
        for name,ident in [('one',11),('two',33)]}
    calls=[]
    def observe(_cluster,name,server):calls.append(server.server_id);return deepcopy(observations[name])
    monkeypatch.setattr(audit,'pacemaker_node',observe)
    return SimpleNamespace(cluster=cluster,second=second,observations=observations,calls=calls,xml=xml)


def run(product_client,state):
    response=product_client.post(f'/ha/api/readiness/pacemaker/{state.cluster.id}/run',json={})
    assert response.status_code==202,response.text
    task=InstallationTasks.get_by_id(response.json['task_id'])
    assert audit.execute(task)=='completed'
    row=OperationalAssessment.get_by_id(response.json['id'])
    assert row.result.startswith('fernet:')
    return row,deserialize_operation_payload(row.result)


def test_audit_reads_every_member_and_never_calls_configured_fencing_tested(product_client,readiness_cluster):
    row,result=run(product_client,readiness_cluster)
    assert readiness_cluster.calls==[11,33]
    checks={check['key']:check for check in result['checks']}
    assert checks['witness']['state']=='observed' and checks['fencing']['state']=='configured'
    assert checks['verification']['state']=='unknown' and result['last_verified_test'] is None
    assert checks['domain:hypervisor']['state']=='unknown'
    assert 'NeverExposeThisSecret' not in json.dumps(result)
    response=product_client.get(f'/ha/api/readiness/pacemaker/{readiness_cluster.cluster.id}')
    assert response.status_code==200 and response.json['assessments'][0]['id']==row.id


@pytest.mark.parametrize('field,value',[('votes',0),('tls',False),('host','192.0.2.88:5403')])
def test_quorate_cluster_with_zero_or_mismatched_witness_evidence_fails(product_client,readiness_cluster,field,value):
    node=readiness_cluster.observations['two']
    node['quorum' if field=='votes' else 'witness'][field]=value
    _row,result=run(product_client,readiness_cluster)
    assert next(c for c in result['checks'] if c['key']=='witness')['state']=='fail'


def test_unreachable_member_never_yields_success_or_raw_ssh_errors(product_client,readiness_cluster,monkeypatch):
    def unavailable(_cluster,name,server):
        if name=='two':raise RuntimeError('PRIVATE SSH CREDENTIAL')
        return deepcopy(readiness_cluster.observations[name])
    monkeypatch.setattr(audit,'pacemaker_node',unavailable)
    _row,result=run(product_client,readiness_cluster)
    assert next(c for c in result['checks'] if c['key']=='members')['state']=='fail'
    assert 'PRIVATE SSH' not in json.dumps(result)


def test_failure_domains_are_group_scoped_and_versioned(product_client,readiness_cluster):
    url=f'/ha/api/readiness/pacemaker/{readiness_cluster.cluster.id}/profile'
    body=dict(revision=0,domains={'11':dict(hypervisor='pve-one'),'33':dict(hypervisor='pve-one')})
    response=product_client.post(url,json=body)
    assert response.status_code==200,response.text
    assert product_client.post(url,json=body).status_code==409
    assert product_client.post(url,json=dict(revision=response.json['profile']['revision'],domains={'22':dict(power='secret')})).status_code==400
    _row,result=run(product_client,readiness_cluster)
    assert next(c for c in result['checks'] if c['key']=='domain:hypervisor')['evidence']['shared']==['pve-one']


def test_operator_test_record_remains_attested_and_becomes_stale_after_drift(product_client,readiness_cluster):
    row,_result=run(product_client,readiness_cluster)
    body=dict(kind='fencing',result='passed',tested_at=utc_iso(utc_now()),note='External ticket with test procedure',assessment_id=row.id)
    url=f'/ha/api/readiness/pacemaker/{readiness_cluster.cluster.id}/attest'
    assert product_client.post(url,json=dict(body,tested_at=utc_iso(utc_now()+timedelta(days=1)))).status_code==400
    assert product_client.post(url,json=body).status_code==200
    _row,result=run(product_client,readiness_cluster)
    assert result['attestations'][0]['source']=='operator-attestation'
    assert result['attestations'][0]['configuration_current'] and result['last_verified_test'] is None
    readiness_cluster.observations['two']['corosync_digest']='c'*64
    _row,result=run(product_client,readiness_cluster)
    assert not result['attestations'][0]['configuration_current']


@pytest.mark.parametrize('change',['identity','role','disabled'])
def test_queued_audit_rechecks_permissions_and_host_identity(product_client,product,readiness_cluster,change):
    response=product_client.post(f'/ha/api/readiness/pacemaker/{readiness_cluster.cluster.id}/run',json={})
    if change=='identity':Server.update(ip='192.0.2.77').where(Server.server_id==33).execute()
    elif change=='role':UserGroups.update(user_role_id=4).where(UserGroups.user_id==product.admin.user_id).execute()
    else:product.admin.enabled=0;product.admin.save()
    task=InstallationTasks.get_by_id(response.json['task_id'])
    assert audit.execute(task)=='failed'
    assert readiness_cluster.calls==[]


def test_cross_group_requests_and_viewer_mutations_do_not_reach_hosts(product_client,product,readiness_cluster):
    other=PacemakerCluster.create(name='private-ha',group_id=2,servers=readiness_cluster.cluster.servers)
    assert product_client.get(f'/ha/api/readiness/pacemaker/{other.id}').status_code==404
    UserGroups.update(user_role_id=4).where(UserGroups.user_id==product.admin.user_id).execute()
    assert product_client.post(f'/ha/api/readiness/pacemaker/{readiness_cluster.cluster.id}/run',json={}).status_code==403
    assert readiness_cluster.calls==[]


def test_duplicate_audits_do_not_flood_the_operations_queue(product_client,readiness_cluster):
    url=f'/ha/api/readiness/pacemaker/{readiness_cluster.cluster.id}/run'
    assert product_client.post(url,json={}).status_code==202
    assert product_client.post(url,json={}).status_code==409
    assert OperationalAssessment.select().count()==1


def test_registered_witness_can_have_its_failure_domains_declared(product_client,product,readiness_cluster):
    witness=Server.create(server_id=44,hostname='Product witness',ip='192.0.2.99',group_id='1',cred_id=11)
    readiness_cluster.cluster.corosync_settings=json.dumps(dict(qdevice_server_id=witness.server_id));readiness_cluster.cluster.save()
    url=f'/ha/api/readiness/pacemaker/{readiness_cluster.cluster.id}'
    assert product_client.get(url).json['profile']['nodes'][-1]['name']=='Witness: Product witness'
    assert product_client.post(url+'/profile',json=dict(revision=0,domains={'11':dict(power='shared'),'44':dict(power='shared')})).status_code==200
    _row,result=run(product_client,readiness_cluster)
    assert next(c for c in result['checks'] if c['key']=='domain:power')['evidence']['shared']==['shared']


def test_real_node_observation_parses_quorum_tls_and_redacts_cib(product,readiness_cluster,monkeypatch):
    from app.modules.service.ha_readiness import pacemaker_node
    def remote(server):
        def command(args,**kwargs):
            if args[0]=='corosync-cmapctl':return 'totem.cluster_name (str) = product-ha'
            if args[0]=='cibadmin':return readiness_cluster.xml
            if args[0]=='python3':return 'b'*64
            if args[0]=='crm_mon':return '<pacemaker-result><summary><current_dc name="one" with_quorum="true"/></summary><nodes/><resources/></pacemaker-result>'
            if args[-1]=='-v':return 'QNetd host: 192.0.2.99:5403\nTLS active: Yes\n'
            return 'Flags: Quorate Qdevice\n 0 1 Qdevice\n'
        return SimpleNamespace(run=command)
    monkeypatch.setattr(audit,'Remote',remote)
    result=pacemaker_node(readiness_cluster.cluster,'one',product.server)
    assert result['quorum']['votes']==1 and result['witness']['tls']
    assert result['witness']['host']=='192.0.2.99:5403'


@pytest.mark.parametrize('attributes',['<instance_attributes id-ref="other"/>','<instance_attributes id="other"><rule id="r"/></instance_attributes>'])
def test_conditional_fencing_coverage_is_never_reported_static(attributes):
    xml='<cib><configuration><resources><primitive id="fence" class="stonith">'+attributes+'</primitive></resources></configuration></cib>'
    result=audit.fencing(xml,{'one','two'})
    assert result['state']=='warning' and result['evidence']['dynamic_or_conditional']


def test_keepalived_duplicate_vip_owner_is_not_healthy(product_client,product,readiness_cluster,monkeypatch):
    cluster=HaCluster.create(name='product-vrrp',group_id=1,syn_flood=0,description='Fixture')
    router=HaClusterRouter.create(cluster_id=cluster,default=1)
    for ident in (11,33):HaClusterSlave.create(cluster_id=cluster,server_id=ident,router_id=router)
    HaClusterVip.create(cluster_id=cluster,router_id=router,vip='192.0.2.100/24')
    monkeypatch.setattr(audit,'keepalived_node',lambda c,n,s:dict(id=s.server_id,name=n,digest='a'*64,active=True,addresses=['192.0.2.100'],includes=True))
    row=audit.enqueue('keepalived',cluster.id,1,product.admin.user_id)
    assert audit.execute(InstallationTasks.get_by_id(row.task_id))=='completed'
    result=audit.get_report(row.id,1)['report'];checks={c['key']:c for c in result['checks']}
    assert checks['vip-ownership']['state']=='fail' and checks['quorum']['state']=='not-applicable'
    assert checks['includes']['state']=='unknown' and result['last_verified_test'] is None
