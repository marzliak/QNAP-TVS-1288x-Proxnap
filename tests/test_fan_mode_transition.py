"""Explicit mode changes bypass cooldown, never hardware/thermal safeguards."""
from unittest import mock

from test_fan_regressions import OfflineCase


class ModeTransitionTests(OfflineCase):
    def test_each_profile_applies_target_on_first_pass_after_full(self):
        for mode in ("auto", "quiet", "normal", "extra"):
            with self.subTest(mode=mode):
                self.config(mode="full")
                observations = []

                def select():
                    self.config(mode=mode)

                def observe():
                    observations.append((self.cpu_writes()[-1], self.rear_writes()[-1]))

                # Fixed passive temperatures; all final effects remain fake hwmon.
                with mock.patch.object(self.m, "cached_policy_temp", side_effect=lambda policy, age: 35.0):
                    self.assertEqual(self.loop([select, observe]), 0)
                settings = self.settings(mode=mode)
                expected = tuple(self.m.duty_to_pwm(self.m.duty_for_temp(35, g)) for g in settings.groups)
                self.assertEqual(observations, [expected])
                self.m.sensor_inventory.clear()
                self.m.source_sensor_inventory.clear()

    def test_mode_transition_still_obeys_hot_and_missing_sensor_safeguards(self):
        settings = self.settings(mode="quiet")
        for group in settings.groups:
            state = self.m.ControlState(applied_duty=100, last_change=self.clock)
            for temperature in (None, float("nan"), group.critical_temp,
                                self.m.emergency_temp(group, settings)):
                with self.subTest(group=group.name, temp=temperature):
                    self.assertEqual(self.m.controlled_duty(group, temperature, 20, settings,
                                                           state, self.clock, mode_changed=True), 100)
            self.assertEqual(self.m.controlled_duty(group, 30, 20, settings, state,
                                                   self.clock, mode_changed=True),
                             self.m.HARDWARE_FLOORS[group.name])

    def test_same_mode_still_holds_then_steps_normal_thermal_cooldown(self):
        settings = self.settings(mode="auto")
        for group, wait, step in zip(settings.groups, (30, 120), (5, 8)):
            state = self.m.ControlState(applied_duty=100, last_change=self.clock)
            self.assertEqual(self.m.controlled_duty(group, 35, group.min_duty, settings,
                                                   state, self.clock + 5), 100)
            self.assertEqual(self.m.controlled_duty(group, 35, group.min_duty, settings,
                                                   state, self.clock + wait), 100 - step)

    def test_first_cycle_failure_does_not_hide_mode_selection_after_recovery(self):
        self.config(mode="full")
        failed = True
        observations = []

        def temperature(policy, age):
            return None if failed and policy == "cpu_package" else 35

        def select_and_recover():
            nonlocal failed
            failed = False
            self.config(mode="quiet")

        def observe():
            observations.append((self.cpu_writes()[-1], self.rear_writes()[-1]))

        with mock.patch.object(self.m, "cached_policy_temp", side_effect=temperature):
            self.assertEqual(self.loop([select_and_recover, observe]), 0)
        self.assertEqual(observations, [(128, 102)])

    def test_failed_transition_is_retried_without_wait_when_sensor_recovers(self):
        self.config(mode="full")
        failed = False
        observations = []

        def temperature(policy, age):
            return None if failed and policy == "cpu_package" else 35

        def select_with_failure():
            nonlocal failed
            failed = True
            self.config(mode="quiet")

        def recover():
            nonlocal failed
            observations.append((self.cpu_writes()[-1], self.rear_writes()[-1]))
            failed = False

        def observe():
            observations.append((self.cpu_writes()[-1], self.rear_writes()[-1]))

        with mock.patch.object(self.m, "cached_policy_temp", side_effect=temperature):
            self.assertEqual(self.loop([select_with_failure, recover, observe]), 0)
        self.assertEqual(observations, [(255, 255), (128, 102)])

    def test_full_quiet_missing_cpu_cannot_lower_either_zone_after_failure(self):
        self.config(mode="full")
        observations = []

        def select():
            self.config(mode="quiet")

        def observe():
            observations.append((self.cpu_writes()[-1], self.rear_writes()[-1]))

        with mock.patch.object(self.m, "cached_policy_temp", side_effect=lambda policy, age: None if policy == "cpu_package" else 35):
            self.assertEqual(self.loop([select, observe]), 0)
        self.assertEqual(observations, [(255, 255)])
