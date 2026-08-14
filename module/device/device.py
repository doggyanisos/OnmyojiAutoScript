
from collections import deque
from datetime import datetime
import time

import numpy as np

# Patch pkg_resources before importing adbutils and uiautomator2
from module.device.pkg_resources import get_distribution
# Just avoid being removed by import optimization
_ = get_distribution

from module.base.utils import get_color
from module.device.env import IS_WINDOWS
from module.base.timer import Timer
from module.config.utils import get_server_next_update
from module.device.app_control import AppControl
from module.device.control import Control
from module.device.platform2 import Platform
from module.device.screenshot import Screenshot
from module.exception import (GameNotRunningError,
                              GameStuckError,
                              GameTooManyClickError,
                              RequestHumanTakeover,
                              EmulatorNotRunningError)
from module.logger import logger


class Device(Platform, Screenshot, Control, AppControl):
    _screen_size_checked = False
    detect_record = set()
    click_record = deque(maxlen=15)
    stuck_timer = Timer(60, count=60).start()
    stuck_timer_long = Timer(300, count=300).start()
    stuck_long_wait_list = ['BATTLE_STATUS_S', 'PAUSE', 'LOGIN_CHECK', 'PREPARE_BEFORE_BATTLE']
    # Hard wall-clock cap (seconds) for how long a button in `stuck_long_wait_list`
    # may keep the 300s exemption from `stuck_timer_long`. Without this cap, a
    # frozen game that still shows and accepts clicks on these buttons keeps
    # resetting `stuck_timer` (and `stuck_timer_long`) via `handle_control_check`
    # on every successful click, so the exemption becomes permanent and the game
    # never restarts. The cap is measured by `_stuck_long_wait_start` (a
    # timestamp) that is NOT reset by clicks, only by a genuine recovery, so it
    # bounds the exemption even when clicks keep coming. Tunable.
    stuck_long_wait_cap = 900
    # Freeze detection: counts time since the displayed frame last changed.
    # Unlike stuck_timer (reset by clicks), this only resets when the actual
    # screen content changes, so it can catch a frozen game that still shows
    # clickable buttons (which otherwise keeps resetting stuck_timer forever).
    # `count=0`: the freeze is confirmed purely by "screen unchanged for 60s",
    # not by a number of check calls.
    # `freeze_change_threshold`: a frame is considered "changed" only when the
    # mean absolute pixel difference (MAE, in 0-255 gray levels) from the
    # previous frame exceeds this value. A frozen screen (even with minor
    # encoding noise) yields a tiny MAE, so it is correctly treated as
    # unchanged; a genuinely changing screen yields a large MAE and resets the
    # timer. This is far more robust than exact-match or bit-change-ratio tests,
    # both of which reset spuriously on flat/low-contrast frozen frames. Tunable.
    freeze_change_threshold = 5
    freeze_timer = Timer(60, count=0).start()

    def __init__(self, *args, **kwargs):
        # Timestamp (time.time()) marking when a button in `stuck_long_wait_list`
        # was first observed stuck. Used to cap the long-wait exemption; reset
        # only on genuine recovery, NOT on clicks, so it survives the
        # click->stuck_record_clear->stuck_record_add cycle of a frozen game.
        self._stuck_long_wait_start = None
        for trial in range(4):
            try:
                super().__init__(*args, **kwargs)
                break
            except EmulatorNotRunningError:
                if trial >= 3:
                    logger.critical('Failed to start emulator after 3 trial')
                    raise RequestHumanTakeover
                # Try to start emulator
                if self.emulator_instance is not None:
                    self.emulator_start()
                else:
                    logger.critical(
                        f'No emulator with serial "{self.config.Emulator_Serial}" found, '
                        f'please set a correct serial'
                    )
                    raise RequestHumanTakeover

        # Auto-fill emulator info
        if IS_WINDOWS and self.config.script.device.emulatorinfo_type == 'auto':
            _ = self.emulator_instance

        self.screenshot_interval_set()
        self._image_batch_cache_frame_id: str | None = None
        self._image_batch_cache: dict[int, dict] = {}
        self._last_freeze_hash = None  # numpy bool array of the last frame, or None

        # Auto-select the fastest screenshot method
        if self.config.script.device.screenshot_method == 'auto':
            self.run_simple_screenshot_benchmark()

    def reset_image_batch_cache(self, frame_id: str | None = None) -> None:
        self._image_batch_cache_frame_id = frame_id
        self._image_batch_cache = {}

    def invalidate_image_batch_cache(self) -> None:
        self.reset_image_batch_cache()

    def get_image_batch_cache(self, target, frame_id: str | None = None) -> dict | None:
        active_frame_id = self.image_frame_id if frame_id is None else frame_id
        if active_frame_id is None:
            return None
        if self._image_batch_cache_frame_id != active_frame_id:
            return None
        return self._image_batch_cache.get(id(target))

    def update_image_batch_cache(self, targets: list, results: list[dict], frame_id: str | None = None) -> None:
        active_frame_id = self.image_frame_id if frame_id is None else frame_id
        if active_frame_id is None:
            return
        if self._image_batch_cache_frame_id != active_frame_id:
            self.reset_image_batch_cache(active_frame_id)
        for target, result in zip(targets, results):
            self._image_batch_cache[id(target)] = dict(result)

    def run_simple_screenshot_benchmark(self):
        """
        Perform a screenshot method benchmark, test 3 times on each method.
        The fastest one will be set into config.
        """
        logger.info('run_simple_screenshot_benchmark')
        # Check resolution first
        # self.resolution_check_uiautomator2()
        # Perform benchmark
        from module.daemon.benchmark import Benchmark
        bench = Benchmark(config=self.config, device=self)
        method = bench.run_simple_screenshot_benchmark()
        # Set
        self.config.script.device.screenshot_method = method
        self.config.save()

    def handle_night_commission(self, daily_trigger='21:00', threshold=30):
        """
        Args:
            daily_trigger (int): Time for commission refresh.
            threshold (int): Seconds around refresh time.

        Returns:
            bool: If handled.
        """
        update = get_server_next_update(daily_trigger=daily_trigger)
        now = datetime.now()
        diff = (update.timestamp() - now.timestamp()) % 86400
        if threshold < diff < 86400 - threshold:
            return False

        # if GET_MISSION.match(self.image, offset=True):
        #     logger.info('Night commission appear.')
        #     self.click(GET_MISSION)
        #     return True

        return False

    def screenshot(self):
        """
        Returns:
            np.ndarray:
        """
        self.stuck_record_check()

        try:
            super().screenshot()
        except RequestHumanTakeover as e:
            raise RequestHumanTakeover

        if self.handle_night_commission():
            super().screenshot()

        # Freeze detection: update the frame-change timer (independent of clicks)
        self.freeze_detection_check()

        self.reset_image_batch_cache(self.image_frame_id)
        return self.image

    def release_during_wait(self):
        # Scrcpy server is still sending video stream,
        # stop it during wait
        # self.config.script.device.screenshot_method = 'scrcpy'
        if self.config.script.device.screenshot_method == 'scrcpy':
            self._scrcpy_server_stop()
        if self.config.Emulator_ScreenshotMethod == 'nemu_ipc':
            self.nemu_ipc_release()

    def stuck_record_add(self, button):
        """
        当你要设置这个时候检测为长时间的时候，你需要在这里添加
        如果取消后，需要在`stuck_record_clear`中清除
        :param button:
        :return:
        """
        self.detect_record.add(str(button))
        # Start the long-wait cap clock the first time a long-wait button is
        # observed stuck. Sticky: not reset by clicks, so it keeps counting
        # across the click->clear->re-add cycle of a frozen game.
        if button in self.stuck_long_wait_list and self._stuck_long_wait_start is None:
            self._stuck_long_wait_start = time.time()
        logger.info(f'Add stuck record: {button}')

    def stuck_record_clear(self, reset_long_wait_cap=True):
        self.detect_record = set()
        self.stuck_timer.reset()
        self.stuck_timer_long.reset()
        # Genuine recovery resets the long-wait cap clock. Clicks must NOT reset
        # it (see `handle_control_check`), otherwise a frozen game that keeps
        # clicking would never hit the cap.
        if reset_long_wait_cap:
            self._stuck_long_wait_start = None

    def _long_wait_exceeded(self):
        """
        Whether a button in `stuck_long_wait_list` has been stuck longer than
        the hard cap `stuck_long_wait_cap`.

        Returns:
            bool: True if the long-wait exemption should be dropped and a
                restart forced.
        """
        in_long_wait = any(button in self.detect_record
                           for button in self.stuck_long_wait_list)
        if not in_long_wait:
            return False
        if self._stuck_long_wait_start is None:
            self._stuck_long_wait_start = time.time()
            return False
        return time.time() - self._stuck_long_wait_start > self.stuck_long_wait_cap

    def stuck_record_check(self):
        """
        Raises:
            GameStuckError:
        """
        reached = self.stuck_timer.reached()

        if not reached:
            # Even if no button is being waited on, a frozen screen (frame not
            # changing for a long time while OAS is still screenshotting) means
            # the game is stuck. This catches the case where clicks keep
            # resetting stuck_timer but the screen itself is dead.
            if getattr(self.config.script.error, 'freeze_detection', False) \
                    and self.freeze_timer.reached():
                logger.warning('Screen appears frozen (no frame change for a long time)')
                # Reset freeze state BEFORE raising, so that the imminent
                # Restart gets a fresh 60s window instead of re-triggering on
                # the very next screenshot (which would burn through the
                # failure limit and abort).
                self.freeze_detection_reset()
                if self.app_is_running():
                    raise GameStuckError('Screen frozen')
                else:
                    raise GameNotRunningError('Game died')
            # Buttons in `stuck_long_wait_list` are allowed to keep the game
            # busy past the normal 60s stuck_timer, but the exemption must not
            # be permanent. A frozen game that still accepts clicks on these
            # buttons keeps resetting stuck_timer via handle_control_check, so
            # the 300s stuck_timer_long never times out. Cap the exemption by
            # wall-clock time the button has actually been stuck; once exceeded,
            # force a restart even though clicks keep coming.
            if self._long_wait_exceeded():
                logger.warning(
                    f'Long-wait button stuck beyond cap '
                    f'({self.stuck_long_wait_cap}s): {self.detect_record}'
                )
            else:
                return False

        logger.warning('Wait too long')
        logger.warning(f'Waiting for {self.detect_record}')
        self.stuck_record_clear()

        if self.app_is_running():
            raise GameStuckError(f'Wait too long')
        else:
            raise GameNotRunningError('Game died')

    def freeze_detection_check(self):
        """
        Update the freeze timer based on whether the displayed frame actually
        changed. The timer is ONLY reset when the screen content changes, so a
        frozen game that still shows clickable buttons (and thus keeps
        resetting stuck_timer via clicks) will still be caught here.
        """
        if not getattr(self.config.script.error, 'freeze_detection', False):
            return
        image = getattr(self, 'image', None)
        if image is None:
            return
        sig = self._freeze_signature(image)
        if sig is None:
            return
        if self._last_freeze_hash is None or sig.shape != self._last_freeze_hash.shape:
            # First frame, or resolution changed: establish a fresh baseline.
            self._last_freeze_hash = sig
            self.freeze_timer.reset()
            return
        # Mean absolute pixel difference (MAE) between frames. A frozen screen
        # (even with minor encoding noise) yields a tiny MAE and is treated as
        # unchanged; a genuinely changing screen yields a large MAE and resets
        # the timer. This is far more robust than exact-match or bit-change-
        # ratio tests, both of which reset spuriously on flat/low-contrast
        # frozen frames.
        mae = float(np.mean(np.abs(sig.astype(np.int16) - self._last_freeze_hash.astype(np.int16))))
        if mae >= self.freeze_change_threshold:
            self._last_freeze_hash = sig
            self.freeze_timer.reset()
        # else: frame essentially unchanged -> leave the freeze timer running

    def freeze_detection_reset(self):
        """
        Reset the freeze detection state. Call this whenever the app is
        restarted/relaunched so that the restart's own loading screens are not
        mistaken for a frozen (unchanging) screen, and so a post-restart
        screenshot does not immediately re-trigger a freeze error.
        """
        self.freeze_timer.reset()
        self._last_freeze_hash = None

    def _freeze_signature(self, image):
        """
        Cheap perceptual fingerprint of a frame, used to detect whether the
        screen content has changed between two consecutive screenshots.
        """
        try:
            h, w = image.shape[:2]
        except Exception:
            return None
        # Downsample by striding; resolution is fixed per session so sizes match
        step = max(1, h // 32)
        small = image[::step, ::step]
        if small.ndim == 3:
            gray = small[..., :3].mean(axis=2)
        else:
            gray = small
        if gray.size == 0:
            return None
        # Return the downsampled grayscale frame (uint8). The caller compares
        # consecutive frames by mean absolute difference (MAE), which is robust
        # to encoding noise on otherwise-identical frames.
        return gray.astype(np.uint8)

    def handle_control_check(self, button):
        # Clicks must NOT reset the long-wait cap clock (`reset_long_wait_cap`
        # left at its default False), otherwise a frozen game that still accepts
        # clicks would keep rearming the exemption and never restart.
        self.stuck_record_clear(reset_long_wait_cap=False)
        self.click_record_add(button)
        self.click_record_check()

    def click_record_add(self, button):
        self.click_record.append(str(button))

    def click_record_clear(self):
        self.click_record.clear()

    def click_record_remove(self, button):
        """
        Remove a button from `click_record`

        Args:
            button (Button):

        Returns:
            int: Number of button removed
        """
        removed = 0
        for _ in range(self.click_record.maxlen):
            try:
                self.click_record.remove(str(button))
                removed += 1
            except ValueError:
                # Value not in queue
                break

        return removed

    def click_record_check(self):
        """
        Raises:
            GameTooManyClickError:
        """
        count = {}
        for key in self.click_record:
            count[key] = count.get(key, 0) + 1
        count = sorted(count.items(), key=lambda item: item[1], reverse=True)
        if count[0][1] >= 10:
            logger.warning(f'Too many click for a button: {count[0][0]}')
            logger.warning(f'History click: {[str(prev) for prev in self.click_record]}')
            self.click_record_clear()
            raise GameTooManyClickError(f'Too many click for a button: {count[0][0]}')
        if len(count) >= 2 and count[0][1] >= 6 and count[1][1] >= 6:
            logger.warning(f'Too many click between 2 buttons: {count[0][0]}, {count[1][0]}')
            logger.warning(f'History click: {[str(prev) for prev in self.click_record]}')
            self.click_record_clear()
            raise GameTooManyClickError(f'Too many click between 2 buttons: {count[0][0]}, {count[1][0]}')

    def disable_stuck_detection(self):
        """
        Disable stuck detection and its handler. Usually uses in semi auto and debugging.
        """
        logger.info('Disable stuck detection')

        def empty_function(*arg, **kwargs):
            return False

        self.click_record_check = empty_function
        self.stuck_record_check = empty_function

    def app_start(self):
        if not self.config.script.error.handle_error:
            logger.critical('No app stop/start, because HandleError disabled')
            logger.critical('Please enable Alas.Error.HandleError or manually login to AzurLane')
            raise RequestHumanTakeover
        super().app_start()
        self.stuck_record_clear()
        self.click_record_clear()

    def app_stop(self):
        if not self.config.script.error.handle_error:
            logger.critical('No app stop/start, because HandleError disabled')
            logger.critical('Please enable Alas.Error.HandleError or manually login to AzurLane')
            raise RequestHumanTakeover
        super().app_stop()
        self.stuck_record_clear()
        self.click_record_clear()

    def wait_app_start_ready(self, timeout: float = 15.0, interval: float = 0.5) -> None:
        """
        在启动app后，等待包名切换成功且画面脱离纯黑状态。

        这里直接调用底层截图方法做静默探测，避免app刚启动时首屏黑场造成成批告警。

        Args:
            timeout: 最大等待秒数。
            interval: 每轮探测的间隔秒数。
        """
        deadline = time.time() + timeout
        screenshot_method = self.screenshot_methods.get(
            self.config.script.device.screenshot_method,
            self.screenshot_adb
        )

        while time.time() < deadline:
            if not self.app_is_running():
                time.sleep(interval)
                continue

            try:
                image = screenshot_method()
            except Exception as e:
                logger.info(f'Wait game start ready: screenshot probe failed: {e}')
                time.sleep(interval)
                continue

            color = get_color(image, area=(0, 0, 1280, 720))
            if sum(color) >= 1:
                logger.info(f'Game start ready, frame color: {color}')
                return

            time.sleep(interval)

        logger.info('Wait game start ready timeout, continue with login flow')


if __name__ == "__main__":
    device = Device(config="oas1")
    # cv2.imshow("imgSrceen", device.screenshot())  # 显示
    # cv2.waitKey(0)
    # cv2.destroyAllWindows()
