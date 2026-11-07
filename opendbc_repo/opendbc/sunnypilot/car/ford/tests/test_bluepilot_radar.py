"""BluePilot (bp-full): Steer_Assist_Data as a one-track radar (sunnypilot/car/ford/bluepilot/radar_ext.py)."""
import unittest

from opendbc.sunnypilot.car.ford.bluepilot.radar_ext import SteerAssistRadar


def msg(d_long, v_long=-1.0, d_lat=0.0, confidence=3):
  return {'CmbbObjDistLong_L_Actl': d_long, 'CmbbObjRelLong_V_Actl': v_long, 'CmbbObjDistLat_L_Actl': d_lat,
          'CmbbObjConfdnc_D_Stat': confidence}


class TestSteerAssistRadar(unittest.TestCase):
  def setUp(self):
    self.radar = SteerAssistRadar()
    self.pts = {}

  def test_track_lifecycle(self):
    self.radar.update(msg(30.0, d_lat=-0.5), self.pts)
    self.assertEqual((self.pts[0].trackId, self.pts[0].dRel, self.pts[0].yRel, self.pts[0].vRel), (0, 30.0, -0.5, -1.0))

    self.radar.update(msg(29.9), self.pts)
    self.assertEqual(self.pts[0].trackId, 0)  # same object

    self.radar.update(msg(29.9, confidence=0), self.pts)
    self.assertEqual(self.pts, {})  # object lost

    self.radar.update(msg(40.0), self.pts)
    self.assertEqual(self.pts[0].trackId, 1)  # a new object is a new track

  def test_jump_is_new_track(self):
    self.radar.update(msg(30.0), self.pts)
    self.radar.update(msg(40.0), self.pts)  # > 5 m jump
    self.assertEqual(self.pts[0].trackId, 1)
    self.radar.update(msg(40.0, v_long=-5.0), self.pts)  # > 2 m/s jump
    self.assertEqual(self.pts[0].trackId, 2)

  def test_speed_from_distance_when_unreported(self):
    self.radar.update(msg(30.0, v_long=0.0), self.pts)
    self.radar.update(msg(30.5, v_long=0.0), self.pts)
    self.radar.update(msg(31.0, v_long=0.0), self.pts)
    self.assertAlmostEqual(self.pts[0].vRel, 1.0, places=4)  # sum of the distance changes
    self.assertEqual(self.pts[0].trackId, 0)


if __name__ == '__main__':
  unittest.main()
