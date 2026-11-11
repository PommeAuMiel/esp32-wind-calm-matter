"""Only the selected Matter node's unused settings and update check may change."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import ha_presentation as presentation

def entity(node, endpoint, kind, domain):
    return {'platform':'matter','unique_id':'FABRIC-'+format(node,'016X')+'-'+str(endpoint)+'-'+kind,
            'entity_id':domain+'.node_'+str(node)+'_'+str(endpoint), 'disabled_by':None}

class Fake:
    def __init__(self):
        self.registry=[entity(n,e,'MatterStartUpOnOff-6-16387','select') for n in (7,8) for e in (1,3)]
        self.registry += [entity(7,0,'MatterUpdate-40-9','update')]
        self.calls=[];self.polls=0
    def call(self, command, **args):
        self.calls.append((command,args))
        if command=='config/entity_registry/list':return self.registry
        if command=='config/entity_registry/update':
            next(e for e in self.registry if e['entity_id']==args['entity_id'])['disabled_by']=args['disabled_by'];return {}
        if command=='call_service':
            assert args['domain']=='homeassistant' and args['service']=='update_entity';return {}
        if command=='get_states':
            self.polls+=1
            return [{'entity_id':'update.node_7_0','state':'unknown' if self.polls==1 else 'off',
                     'attributes':{'installed_version':'4.0.4-thread-ota','latest_version':None if self.polls==1 else '4.0.4-thread-ota'}}]
        raise AssertionError(command)

class PresentationTests(unittest.TestCase):
    def test_tidy_exactly_two_unused_settings_idempotently(self):
        client=Fake();result=presentation.tidy(7,client)
        self.assertEqual(len(result['changed_entities']),2)
        self.assertTrue(all(e['disabled_by'] is None for e in client.registry if '-0000000000000008-' in e['unique_id']))
        self.assertEqual(presentation.tidy(7,client)['changed_entities'],[])
    def test_validate_both_targets_before_writing(self):
        client=Fake();client.registry.append(client.registry[1].copy())
        with self.assertRaises(ValueError):presentation.tidy(7,client)
        self.assertTrue(all(cmd=='config/entity_registry/list' for cmd,_ in client.calls))
    def test_malformed_registry_and_states_cannot_trigger_install_or_other_changes(self):
        client=Fake();client.registry[1]['entity_id']=None
        with self.assertRaises(ValueError):presentation.tidy(7,client)
        self.assertTrue(all(cmd=='config/entity_registry/list' for cmd,_ in client.calls))
        class InvalidStates(Fake):
            def call(self, command, **args):
                if command=='get_states':return {'private':'invalid'}
                return super().call(command,**args)
        invalid=InvalidStates()
        result=presentation.refresh_firmware(7,'4.0.4-thread-ota',invalid,lambda _:None)
        self.assertFalse(result['success']);self.assertFalse(result['installation_requested'])
        self.assertEqual(sum(cmd=='call_service' for cmd,_ in invalid.calls),1)
    def test_refresh_check_never_installs_and_handles_initial_unknown(self):
        client=Fake();sleeps=[]
        result=presentation.refresh_firmware(7,'4.0.4-thread-ota',client,sleeps.append)
        self.assertTrue(result['success']);self.assertEqual(result['state'],'off');self.assertFalse(result['installation_requested'])
        self.assertEqual(sleeps,[1])
        self.assertEqual(sum(cmd=='call_service' for cmd,_ in client.calls),1)
    def test_different_installed_version_remains_unconfirmed_without_retry(self):
        client=Fake();result=presentation.refresh_firmware(7,'other',client,lambda _:None)
        self.assertFalse(result['success']);self.assertEqual(client.polls,4)
        self.assertEqual(sum(cmd=='call_service' for cmd,_ in client.calls),1)
