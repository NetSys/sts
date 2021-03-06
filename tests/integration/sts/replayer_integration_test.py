#!/usr/bin/env python

import unittest
import sys
import os
import itertools
from copy import copy
import types
import tempfile

sys.path.append(os.path.dirname(__file__) + "/../../..")

simple_cfg = '''
from sts.control_flow import Replayer
from sts.simulation_state import SimulationConfig

simulation_config = SimulationConfig()
control_flow = Replayer(simulation_config, "%s")
'''

class ReplayerTest(unittest.TestCase):
  tmpsuperlog = '/tmp/superlog.tmp'
  tmpcfg = 'config/replayer_simple_test.py'
  tmpcfgpyc = 'config/replayer_simple_test.pyc'
  tmpcfgmodule = 'config.replayer_simple_test'

  def write_simple_superlog(self):
    ''' Returns the file. Make sure to close afterwards! '''
    superlog = open(self.tmpsuperlog, 'w')
    e1 = str('''{"dependent_labels": ["e2"], "start_dpid": 8, "class": "LinkFailure",'''
             ''' "start_port_no": 3, "end_dpid": 15, "end_port_no": 2, "label": "e1", "time": [0,0], "round": 0}''')
    superlog.write(e1 + '\n')
    e2 = str('''{"dependent_labels": [], "start_dpid": 8, "class": "LinkRecovery",'''
             ''' "start_port_no": 3, "end_dpid": 15, "end_port_no": 2, "label": "e2", "time": [0,0], "round": 0}''')
    superlog.write(e2 + '\n')
    superlog.close()

  def write_simple_cfg(self):
    cfg = open(self.tmpcfg, 'w')
    cfg.write(simple_cfg % self.tmpsuperlog)
    cfg.close()

  def basic_test(self):
    try:
      self.write_simple_superlog()
      self.write_simple_cfg()
      ret = os.system("./simulator.py -c %s" % self.tmpcfgmodule)
      self.assertEqual(0, ret)
    finally:
      os.unlink(self.tmpsuperlog)
      os.unlink(self.tmpcfg)
      if os.path.exists(self.tmpcfgpyc):
        os.unlink(self.tmpcfgpyc)

if __name__ == '__main__':
  unittest.main()
