from lxml import etree

from module.device.method.adb import Adb
from module.device.method.uiautomator_2 import Uiautomator2
from module.device.method.utils import HierarchyButton
# from module.device.method.wsa import WSA
from module.logger import logger


class AppControl(Adb, Uiautomator2):
    hierarchy: etree._Element
    _app_u2_family = ['uiautomator2', 'minitouch', 'scrcpy']

    # 华为渠道弹窗包（活在 Android/模拟器层，会挡在游戏登录界面之前）。
    # 杀游戏进程清不掉它们，需在重启游戏时一并 am force-stop。
    _CHANNEL_POPUP_PACKAGES = (
        'com.huawei.appmarket',       # 华为应用市场
        'com.huawei.hwid',            # 华为账号
        'com.huawei.systemmanager',   # 华为系统管理 / 权限弹窗
        'com.huawei.hms',             # 华为移动服务
    )

    def app_is_alive(self, package_name=None) -> bool:
        """
        判断目标应用进程是否仍然存活，不要求当前位于前台。

        这用于区分“应用被切到后台”和“应用已经被真正杀掉”两种情况。
        """
        if not package_name:
            package_name = self.package

        try:
            result = self.adb_shell(['pidof', package_name])
        except Exception as e:
            logger.info(f'Check app alive by pidof failed: {e}')
            return False

        result = result.strip(' \t\r\n')
        logger.attr('Package_pid', result if result else 'None')
        return bool(result)

    def app_is_running(self) -> bool:
        method = self.config.script.device.control_method
        # if self.is_wsa:
        #     package = self.app_current_wsa()
        if method in AppControl._app_u2_family:
            package = self.app_current_uiautomator2()
        else:
            package = self.app_current_adb()

        package = package.strip(' \t\r\n')
        logger.attr('Package_name', package)
        return package == self.package

    def app_start(self):
        method = self.config.script.device.screenshot_method
        logger.info(f'App start: {self.package}')
        # if self.config.Emulator_Serial == 'wsa-0':
        #     self.app_start_wsa(display=0)
        if method in AppControl._app_u2_family:
            self.app_start_uiautomator2()
        else:
            self.app_start_adb()

    def app_stop(self):
        method = self.config.script.device.screenshot_method
        logger.info(f'App stop: {self.package}')
        if method in AppControl._app_u2_family:
            self.app_stop_uiautomator2()
        else:
            self.app_stop_adb()
        # 渠道弹窗（华为应用市场 / 账号 / 系统管理 / HMS）活在 Android 层，
        # 仅杀游戏清不掉，会挡在登录界面导致脚本反复乱点。杀游戏后顺手
        # force-stop 这些弹窗包（不动模拟器）。
        self.kill_channel_popups()

    def _current_foreground_package(self) -> str:
        """
        返回当前前台包名（best-effort）。取不到时返回空串。
        """
        method = self.config.script.device.screenshot_method
        try:
            if method in AppControl._app_u2_family:
                return self.app_current_uiautomator2()
            else:
                return self.app_current_adb()
        except Exception as e:
            logger.info(f'Get foreground package failed: {e}')
            return ''

    def kill_channel_popups(self):
        """
        若前台正被已知华为渠道弹窗包占据，则 am force-stop 之。

        华为渠道弹窗（应用市场 / 账号 / 系统管理 / HMS）活在 Android/模拟器
        层，仅 restart 游戏（杀游戏进程）清不掉它，于是登录界面被挡住、
        OAS 在登录流程里反复点不到真实按钮（"乱点"），再被冻结检测判死 →
        重启游戏 → 弹窗仍在 → 又乱点，无限循环。在此处 force-stop 掉弹窗
        包即可在不重启模拟器的前提下解开，两条重启路径（Login 重试、Restart
        恢复）都经过 app_stop，故都会被覆盖。
        """
        fg = self._current_foreground_package()
        if not fg:
            return
        for pkg in AppControl._CHANNEL_POPUP_PACKAGES:
            if fg == pkg or fg.startswith(pkg + '.'):
                logger.warning(f'Channel popup detected in foreground ({pkg}), force-stopping it')
                try:
                    self.adb_shell(['am', 'force-stop', pkg])
                    logger.info(f'Force-stopped channel popup: {pkg}')
                except Exception as e:
                    logger.warning(f'Force-stop channel popup failed: {e}')
                break

    def dump_hierarchy(self) -> etree._Element:
        """
        Returns:
            etree._Element: Select elements with `self.hierarchy.xpath('//*[@text="Hermit"]')` for example.
        """
        method = self.config.script.device.screenshot_method
        if method in AppControl._app_u2_family:
            self.hierarchy = self.dump_hierarchy_uiautomator2()
        else:
            self.hierarchy = self.dump_hierarchy_adb()
        return self.hierarchy

    def xpath_to_button(self, xpath: str) -> HierarchyButton:
        """
        Args:
            xpath (str):

        Returns:
            HierarchyButton:
                An object with methods and properties similar to Button.
                If element not found or multiple elements were found, return None.
        """
        return HierarchyButton(self.hierarchy, xpath)
