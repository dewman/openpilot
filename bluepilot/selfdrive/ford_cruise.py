"""Keep a new stock ACC engagement from racing selfdrived's acknowledgement."""


class FordCruiseCancelBP:
  # At 100 Hz this allows at most five control cycles for the matching state.
  ACK_TIMEOUT_NS = 50_000_000

  def __init__(self):
    self.cruise_enabled = False
    self.engagement_time = 0

  def update(self, cancel, CS, car_state_time, selfdrive_state_time, now, valid):
    """Defer only cancellation based on a state published before ACC engaged.

    A newer selfdrived decision, a pedal/fault, invalid input, or expiry always
    preserves cancellation. This never enables assistance or sends RESUME/SET.
    Timestamps use the same monotonic nanosecond clock as cereal messages.
    """
    if CS.cruiseState.enabled and not self.cruise_enabled:
      self.engagement_time = car_state_time
    self.cruise_enabled = CS.cruiseState.enabled

    waiting_for_ack = (
      valid and self.cruise_enabled and
      0 < selfdrive_state_time < self.engagement_time <= car_state_time <= now and
      now - selfdrive_state_time < self.ACK_TIMEOUT_NS and
      now - self.engagement_time < self.ACK_TIMEOUT_NS and
      not (CS.brakePressed or CS.regenBraking or CS.accFaulted or
           CS.steerFaultTemporary or CS.steerFaultPermanent)
    )
    return cancel and not waiting_for_ack
