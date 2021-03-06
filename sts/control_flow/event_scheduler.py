import time

from sts.replay_event import *

import logging
log = logging.getLogger("event_scheduler")
from collections import Counter
import operator

def format_time(time):
  mins = int(time/60)
  secs = int(time % 60)
  ms = int( (time * 1000) % 1000)
  return "%02d:%02d.%03d" % (mins, secs, ms)

class EventSchedulerStats(object):
  def __init__(self):
    self.event2matched = Counter()
    self.event2timeouts = Counter()
    self.replay_start = None
    self.record_start = None

  def start_replay(self, event):
    self.replay_start = time.time()
    self.record_start = event.time.as_float()

  def time(self, event):
    return format_time(time.time() - self.replay_start) + " " + \
           format_time(event.time.as_float() - self.record_start)

  def event_matched(self, event):
    msg.event_success(self.time(event) + " Sucessfully matched event "+str(event))
    # TODO(cs): maybe want more info than just class name? (e.g. fingerprint)
    self.event2matched[event.__class__.__name__] += 1

  def event_timed_out(self, event):
    msg.event_timeout(self.time(event) + " Event timed out "+str(event))
    self.event2timeouts[event.__class__.__name__] += 1

  def sorted_match_counts(self):
    for e, count in sorted(self.event2matched.items(),
                           key=operator.itemgetter(1)):
      yield (e, count)

  def sorted_timeout_counts(self):
    for e, count in sorted(self.event2timeouts.items(),
                           key=operator.itemgetter(1)):
      yield (e, count,)

  def __str__(self):
    total_matched = sum(self.event2matched.values())
    total_timeouts = sum(self.event2timeouts.values())
    s = []
    s.append("Events matched: %d, timed out: %d\n" % (total_matched,
                                                      total_timeouts))
    s.append("Matches per event type:\n")
    for e, count in self.sorted_match_counts():
      s.append("  %s %d\n" % (e, count,))
    s.append("Timeouts per event type:\n")
    for e, count in self.sorted_timeout_counts():
      s.append("  %s %d\n" % (e, count,))
    return "".join(s)

class DumbEventScheduler(object):

  kwargs = set(['epsilon_seconds', 'sleep_interval_seconds'])

  def __init__(self, simulation, epsilon_seconds=0.0, sleep_interval_seconds=0.2):
    self.simulation = simulation
    self.epsilon_seconds = epsilon_seconds
    self.sleep_interval_seconds = sleep_interval_seconds
    self.last_event = None
    self.stats = EventSchedulerStats()

  def schedule(self, event):
    if self.last_event:
      rec_delta = (event.time.as_float() - self.last_event.time.as_float())
      if rec_delta > 0:
        log.info("Sleeping for %.0f ms before next event" % (rec_delta * 1000))
        self.simulation.io_master.sleep(rec_delta)
    else:
      self.stats.start_replay(event)

    log.debug("Waiting for %s (maximum wait time: %.0f ms)" %
          ( str(event).replace("\n", ""), self.epsilon_seconds * 1000) )

    proceed = False
    while True:
      now = time.time()
      if event.proceed(self.simulation):
        proceed = True
        break
      elif now > end:
        break
      self.simulation.io_master.select(self.sleep_interval_seconds)
    if proceed:
      self.stats.event_matches(event)
    else:
      self.stats.event_timed_out(event)
    self.last_event = event

class EventScheduler(object):
  '''an EventWatchers schedules events. It controls their admission, and
  any post-event delay '''

  kwargs = set(['speedup', 'delay_input_events', 'initial_wait',
                'epsilon_seconds', 'sleep_interval_seconds'])

  def __init__(self, simulation, speedup=1.0,
               delay_input_events=True, initial_wait=0.5, epsilon_seconds=0.5,
               sleep_interval_seconds=0.2):
    self.simulation = simulation
    self.speedup = speedup
    self.delay_input_events = delay_input_events
    self.last_real_time = None
    self.last_rec_time = None
    self.initial_wait = initial_wait
    self.epsilon_seconds = epsilon_seconds
    self.sleep_interval_seconds = sleep_interval_seconds
    self.started = False
    self.stats = EventSchedulerStats()

  def schedule(self, event):
    if not self.started:
      self.stats.start_replay(event)
      self.started = True

    if isinstance(event, InputEvent):
      self.inject_input(event)
    else:
      self.wait_for_internal(event)
    self.update_event_time(event)

  def inject_input(self, event):
    if self.delay_input_events:
      wait_time_seconds = self.wait_time(event)
      if wait_time_seconds > 0.01:
        log.debug("Delaying input_event %s for %.0f ms" %
            ( str(event).replace("\n", "") , (wait_time_seconds) * 1000 ))

        self.simulation.io_master.sleep(wait_time_seconds)
    log.debug("Injecting %r", event)
    # TODO(cs): AFACT, this is essentially a dummy variable? Since event.time
    # is in the past... Andi, can you verify this?
    end = event.time.as_float()
    self._poll_event(event, end)

  def wait_for_internal(self, event):
    wait_time_seconds = self.wait_time(event)
    start = time.time()
    # TODO(cs): why - 0.01?
    end = start + wait_time_seconds - 0.01 + self.epsilon_seconds
    if event.timeout_disallowed:
      # Reaallllly far in the future
      end = 30000000000 # Fri, 30 Aug 2920 05:20:00 GMT
      log.debug("Waiting for %s forever" %
                ( repr(event).replace("\n", "")))
    else:
      log.debug("Waiting for %s (maximum wait time: %.0f ms)" %
            ( repr(event).replace("\n", ""), self.epsilon_seconds * 1000) )
    self._poll_event(event, end)

  def _poll_event(self, event, end_time):
    proceed = False
    while True:
      now = time.time()
      if event.proceed(self.simulation):
        proceed = True
        break
      elif now > end_time:
        break
      self.simulation.io_master.select(self.sleep_interval_seconds)
    if proceed:
      self.stats.event_matched(event)
      self.update_event_time(event)
    else:
      self.stats.event_timed_out(event)

  def update_event_time(self, event):
    """ update events """
    self.last_real_time = time.time()
    self.last_rec_time = event.time

  def wait_time(self, event):
    """ returns how long to wait in seconds for an event to occur or be injected. """
    if not self.last_real_time:
      return self.initial_wait

    rec_delta = (event.time.as_float() - self.last_rec_time.as_float()) / self.speedup
    real_delta = time.time() - self.last_real_time

    to_wait = rec_delta - real_delta
    if to_wait > 10000:
      raise RuntimeError("to_wait %d ms is way too big for event %s" %
                         (to_wait, str(event)))
    return max(to_wait, 0)
