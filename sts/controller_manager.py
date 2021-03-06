from sts.util.console import msg

class ControllerManager(object):
  ''' Encapsulate a list of controllers objects '''
  def __init__(self, controllers):
    self.cid2controller = {
      controller.cid : controller
      for controller in controllers
    }

  @property
  def controller_configs(self):
    return [ c.config for c in self.controllers ]

  @property
  def controllers(self):
     cs = self.cid2controller.values()
     cs.sort(key=lambda c: c.cid)
     return cs

  @property
  def live_controllers(self):
    alive = [controller for controller in self.controllers if controller.alive]
    return set(alive)

  @property
  def down_controllers(self):
    down = [controller for controller in self.controllers if not controller.alive]
    return set(down)

  def get_controller_by_label(self, label):
    for c in self.cid2controller.values():
      if c.label == label:
        return c
    return None

  def get_controller(self, cid):
    if cid not in self.cid2controller:
      raise ValueError("unknown cid %s" % str(cid))
    return self.cid2controller[cid]

  def kill_all(self):
    for c in self.live_controllers:
      c.kill()
    self.cid2controller = {}

  @staticmethod
  def kill_controller(controller):
    msg.event("Killing controller %s" % str(controller))
    controller.kill()

  @staticmethod
  def reboot_controller(controller):
    msg.event("Restarting controller %s" % str(controller))
    controller.start()

  def check_controller_processes_alive(self):
    controllers_with_problems = []
    live = list(self.live_controllers)
    live.sort(key=lambda c: c.cid)
    for c in live:
      (rc, msg) = c.check_process_status()
      if not rc:
        c.alive = False
        controllers_with_problems.append ( (c, msg) )
    return controllers_with_problems
