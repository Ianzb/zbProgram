from .interface import *


class Window(zbw.Window):
    """
    主窗口
    """
    initFinished = pyqtSignal()

    addAddonEvent = pyqtSignal(dict)
    removeAddonEvent = pyqtSignal(dict)
    addAddonFinishEvent = pyqtSignal(str)
    downloadAddonFailedSignal = pyqtSignal(dict)
    showSignal = pyqtSignal()

    def __init__(self):
        super().__init__()

        sys.excepthook = self.errorHook

        # 托盘组件
        self.tray = Tray(self)

        self.mainPage = MainPage(self)
        self.settingPage = SettingPage(self)
        self.aboutPage = AboutPage(self)
        self.addPage(self.mainPage, self.mainPage.title(), self.mainPage.icon(), "top")
        self.addSeparator("top")
        self.addSeparator("bottom")
        self.addPage(self.settingPage, self.settingPage.title(), self.settingPage.icon(), "bottom")
        self.addPage(self.aboutPage, self.aboutPage.title(), self.aboutPage.icon(), "bottom")

        # 循环监测事件
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.timerEvent)
        self.timer.start(250)

        self.addAddonEvent.connect(self.addAddon)
        self.removeAddonEvent.connect(self.removeAddon)
        self.downloadAddonFailedSignal.connect(self.__downloadAddonFailed)

        # 外观调整
        self.navigationInterface.setAcrylicEnabled(True)
        # 窗口属性
        self.setMinimumSize(700, 500)
        self.resize(900, 700)
        # 窗口居中
        desktop = QApplication.screens()[0].size()
        w, h = desktop.width(), desktop.height()
        self.move(w // 2 - self.width() // 2, h // 2 - self.height() // 2)

        self.setWindowIcon(QIcon(program.ICON))
        self.setWindowTitle(program.TITLE)
        self.navigationInterface.setReturnButtonVisible(False)

        # 任务中心
        self.progressCenterFlyout = None
        self.progressCenter = ProgressCenter(self)

        self.progressCenterButton = TransparentToolButton(ZBF.apps_list, self)
        self.progressCenterButton.setFixedSize(46, 32)
        self.progressCenterButton.clicked.connect(lambda: self.showProgressCenter(FlyoutAnimationType.DROP_DOWN))

        self.titleBar.buttonLayout.insertWidget(0, self.progressCenterButton)

        # 设置数据异常提醒
        if setting.errorState:
            self.infoBar = InfoBar(InfoBarIcon.ERROR, "错误", "设置文件数据错误，已自动恢复至默认选项，具体错误原因请查看程序日志！", Qt.Orientation.Vertical, True, -1, InfoBarPosition.TOP_RIGHT, self.mainPage)
            self.infoBar.show()
        self.initFinished.emit()
        self.showSignal.emit()

        # 插件安装
        data = addonManager.getInstalledAddonInfo()
        for i in data.keys():
            self.addAddon(data[i])

    def errorHook(self, exc_type, exc_value, exc_traceback):
        errorMessageBox = ErrorMessageBox("程序发生错误", "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)), self)
        errorMessageBox.show()

    def showProgressCenter(self, aniType=FlyoutAnimationType.DROP_DOWN):
        if self.progressCenterFlyout is None:
            self.progressCenterFlyout = Flyout.make(self.progressCenter, self.progressCenterButton, self, aniType=aniType, isDeleteOnClose=False)
        else:
            self.progressCenterFlyout.close()
            # 注：使用deleteLater会导致ToolTip被删除，进而报错，并且此处有内存泄露
            del self.progressCenterFlyout
            self.progressCenterFlyout = Flyout.make(self.progressCenter, self.progressCenterButton, self, aniType=aniType, isDeleteOnClose=False)

    def showEvent(self, a0):
        self.showSignal.emit()
        super().showEvent(a0)

    def closeEvent(self, QCloseEvent):
        """
        自定义关闭事件
        """
        QCloseEvent.ignore()
        if setting.read("hideWhenClose"):
            self.hide()
        else:
            program.close()

    def downloadAddon(self, data):
        # 任务卡必须在主线程创建；工作线程内仅通过信号安全接口更新
        name = data.get("name", data.get("id", "未知插件"))
        card = self.progressCenter.addTask(True, True, False, False, False)
        card.setTitle(f"安装插件：{name}")
        card.setText("准备安装...")
        card.start()
        self.__downloadAddon(data, card)

    @zb.threadPoolDecorator(program.THREAD_POOL)
    def __downloadAddon(self, data, card):
        try:
            result = addonManager.downloadAddonFromInfo(data, progress=card.setText)
            self.addAddonFinishEvent.emit(data.get("id"))
            if result:
                card.setText("安装完成")
                card.finish(True)
                self.addAddonEvent.emit(data)
            else:
                card.setText("安装失败")
                card.finish(False)
                self.downloadAddonFailedSignal.emit(data)
        except Exception:
            logging.warning(f"插件{data.get("name")}安装任务异常：{traceback.format_exc()}")
            card.setText("安装失败")
            card.finish(False)
            self.downloadAddonFailedSignal.emit(data)

    def __downloadAddonFailed(self, data):
        self.infoBar = InfoBar(InfoBarIcon.ERROR, "错误", f"插件{data.get("name")}下载失败！", Qt.Orientation.Vertical, True, 10000, InfoBarPosition.TOP_RIGHT, self.mainPage)
        self.infoBar.show()

    def addAddon(self, info: dict):
        """
        添加插件
        @param info: 数据
        """
        try:
            lib = None
            # 是否为重装/更新：安装前该插件已在运行（程序启动时首次加载则不在）
            reinstalled = info.get("id") in addonManager.ADDON_OBJECT
            # 卸载已安装插件
            if reinstalled:
                lib = addonManager.ADDON_OBJECT.pop(info.get("id"), None)
                main_page = addonManager.ADDON_MAIN_PAGE.pop(info.get("id"), None)
                old_info = lib.addonBase.addon_info
                if old_info.get("api_version", 0) >= 5:
                    lib.addonDelete()
                if main_page:
                    self.navigationInterface.removeWidget(main_page.title())
                    self.stackedWidget.view.removeWidget(main_page)
                    main_page.deleteLater()
            # 导入插件
            importlib.invalidate_caches()
            lib = importlib.import_module(info.get("id"))
            lib = importlib.reload(lib)
            addonManager.ADDON_OBJECT[info.get("id")] = lib
            # 初始化插件
            lib.addonBase.set(program, setting, manager, self, self.progressCenter, info)
            lib.addonInit()
            widget = lib.addonWidget()
            addonManager.ADDON_MAIN_PAGE[info.get("id")] = widget
            self.addPage(widget, widget.title(), widget.icon(), "scroll")

            self.infoBar = InfoBar(InfoBarIcon.SUCCESS, "提示", f"插件{info.get("name")}安装成功！", Qt.Orientation.Vertical, True, 5000, InfoBarPosition.TOP_RIGHT, self.mainPage)
            self.infoBar.show()
            logging.info(f"插件{info.get("name")}安装成功")

            # 重装/更新后需重启程序才能完全生效：对所有插件统一提示重启
            if reinstalled:
                box = MessageBox(
                    "插件已更新",
                    f"插件「{info.get('name', info.get('id', '未知插件'))}」已重新安装或更新，"
                    "需重启程序才能完全生效。\n是否现在重启？",
                    self,
                )
                box.yesButton.setText("重启程序")
                box.cancelButton.setText("稍后")
                if box.exec() == MessageBox.Accepted:
                    program.restart()
        except:
            if info.get("api_version", 0) == program.ADDON_API_VERSION:
                self.infoBar = InfoBar(InfoBarIcon.ERROR, "错误", f"插件{info.get("name")}安装失败！", Qt.Orientation.Vertical, True, 10000, InfoBarPosition.TOP_RIGHT, self.mainPage)
            else:
                self.infoBar = InfoBar(InfoBarIcon.ERROR, "错误", f"插件{info.get("name")}与当前程序版本不兼容！", Qt.Orientation.Vertical, True, 10000, InfoBarPosition.TOP_RIGHT, self.mainPage)
            self.infoBar.show()
            logging.warning(f"插件{info.get("name")}安装失败{traceback.format_exc()}")

    def removeAddon(self, info: dict):
        """
        移除插件
        @param info: 数据
        """
        name = info.get("name", info.get("id", "未知插件"))
        card = self.progressCenter.addTask(False, True, False, False, False)
        card.setTitle(f"卸载插件：{name}")
        card.setText("正在移除插件文件...")
        card.start()
        try:
            if info.get("id") in addonManager.ADDON_OBJECT:
                lib = addonManager.ADDON_OBJECT.pop(info.get("id"), None)
                main_page = addonManager.ADDON_MAIN_PAGE.pop(info.get("id"), None)
                old_info = lib.addonBase.addon_info
                if old_info.get("api_version", 0) >= 5:
                    lib.addonDelete()
                if main_page:
                    self.navigationInterface.removeWidget(main_page.title())
                    self.stackedWidget.view.removeWidget(main_page)
                    main_page.deleteLater()
            zb.deleteDir(zb.joinPath(program.ADDON_PATH, info.get("id")), force=True)
            if not zb.existPath(zb.joinPath(program.ADDON_PATH, info.get("id"))):
                logging.info(f"插件{info.get("name")}删除成功")

                self.infoBar = InfoBar(InfoBarIcon.SUCCESS, "提示", f"插件{info.get("name")}删除成功！", Qt.Orientation.Vertical, True, 10000, InfoBarPosition.TOP_RIGHT, self.mainPage)
                self.infoBar.show()
                card.setText("卸载完成")
                card.finish(True)
            else:
                card.setText("卸载失败：文件删除不彻底")
                card.finish(False)
                raise "删除插件失败"

        except:
            logging.info(f"插件{info.get("name")}删除失败")

            self.infoBar = InfoBar(InfoBarIcon.ERROR, "错误", f"插件{info.get("name")}删除失败！", Qt.Orientation.Vertical, True, 10000, InfoBarPosition.TOP_RIGHT, self.mainPage)
            self.infoBar.show()
            card.setText("卸载失败")
            card.finish(False)

    def timerEvent(self):
        """
        重复运行展示窗口
        """
        if zb.existPath(zb.joinPath(program.DATA_PATH, "zb.unlock")):
            zb.deletePath(zb.joinPath(program.DATA_PATH, "zb.unlock"))
            self.show()


logging.debug("程序主窗口类初始化成功！")
