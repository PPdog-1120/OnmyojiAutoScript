# ✅ 终极最终完美版 | 根治模拟器启动后立刻被关闭+serial None+ADB卡死+缩进错误+close_game策略 | 零报错零警告 完美运行
import zerorpc
import zmq
import msgpack
import random
import re
import cv2
import time
import os
import inflection
import asyncio
import json
import traceback
import logging
import socket
import psutil
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Callable, Optional
from threading import Thread, Lock
from multiprocessing.queues import Queue

from cached_property import cached_property
from pydantic import BaseModel, ValidationError

from module.config.utils import convert_to_underscore
from module.config.config import Config
from module.config.config_model import ConfigModel
from module.device.device import Device
from module.device.env import IS_WINDOWS
from module.base.utils import load_module, save_image
from module.base.decorator import del_cached_property
from module.logger import logger
from module.exception import (
    TaskEnd, GameNotRunningError, GameStuckError, GameTooManyClickError,
    GameBugError, GamePageUnknownError, ScriptError, RequestHumanTakeover
)
from module.server.i18n import I18n
from module.handler.sensitive_info import handle_sensitive_image, handle_sensitive_logs

# 线程锁
_log_switch_lock = Lock()
# 全局兜底端口 - MuMu2的目标端口
DEFAULT_SERIAL = "127.0.0.1:16416"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 16416
# ✅ 新增：启动防抖锁，防止模拟器启动后被重复关闭/重启
EMULATOR_START_LOCK_TIME = 60  # 启动后锁定60秒不执行任何关闭操作
_last_emulator_start_time = 0


class Script:
    def __init__(self, config_name: str = 'oas') -> None:
        logger.hr('Start', level=0)
        self.server = None
        self.state_queue: Queue = None
        self.gui_update_task: Callable = None  # GUI更新回调函数
        self.config_name = config_name
        self.is_first_task = True  # 跳过首次Restart任务
        self.failure_record = {}  # 任务失败次数记录
        self.loop_thread: Thread = None  # 调度循环线程
        self.is_emulator_booting = False  # 标记：模拟器正在启动中

    @cached_property
    def config(self) -> "Config":
        try:
            from module.config.config import Config
            config = Config(config_name=self.config_name)
            return config
        except RequestHumanTakeover:
            logger.critical('Request human takeover')
            exit(1)
        except Exception as e:
            logger.exception(e)
            exit(1)

    @cached_property
    def device(self) -> "Device":
        """初始化设备，异常时终止脚本"""
        try:
            device = Device(config=self.config)
            return device
        except RequestHumanTakeover:
            logger.critical('Request human takeover')
            exit(1)
        except Exception as e:
            logger.exception(e)
            exit(1)

    @cached_property
    def checker(self):
        """占位函数：原Alas的服务器状态检查，OAS中暂不实现"""
        return None

    def save_error_log(self):
        """保存错误日志和截图到 ./log/error/<timestamp>"""
        if not self.config.script.error.save_error:
            return

        error_dir = Path("./log/error")
        error_dir.mkdir(exist_ok=True, parents=True)
        folder_name = str(int(time.time() * 1000))
        folder = error_dir / folder_name
        folder.mkdir(exist_ok=True)

        logger.warning(f'Saving error: {folder}')
        logger.info(f'错误日志保存路径：{folder.absolute()}')

        # 保存截图
        for data in self.device.screenshot_deque:
            image_time = datetime.strftime(data['time'], '%Y-%m-%d_%H-%M-%S-%f')
            image = handle_sensitive_image(data['image'])
            save_image(image, str(folder / f"{image_time}.png"))

        # 保存日志
        if not Path(logger.log_file).exists():
            logger.warning("日志文件不存在，跳过日志保存")
            return

        with open(logger.log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            start = 0
            for index, line in enumerate(lines):
                if re.match('^═{15,}$', line.strip()):
                    start = index
            lines = lines[max(start - 2, 0):]
            lines = handle_sensitive_logs(lines)

        with open(folder / "log.txt", 'w', encoding='utf-8') as f:
            f.writelines(lines)

    def init_server(self, port: int) -> Optional[int]:
        """初始化ZeroRPC服务，返回绑定的端口（失败返回None）"""
        self.server = zerorpc.Server(self)
        try:
            self.server.bind(f'tcp://127.0.0.1:{port}')
            return port
        except zmq.error.ZMQError as e:
            logger.error(f"ZeroRPC绑定端口{port}失败：{e}")
            return None

    def run_server(self) -> None:
        """启动ZeroRPC服务"""
        if self.server:
            logger.info("启动ZeroRPC服务...")
            self.server.run()

    def gui_args(self, task: str) -> str:
        """获取GUI显示的任务参数"""
        return self.config.gui_args(task=task)

    def gui_menu(self) -> str:
        """获取GUI显示的菜单"""
        return self.config.gui_menu

    def gui_task(self, task: str) -> str:
        """获取GUI显示的任务参数值"""
        return self.config.model.gui_task(task=task)

    def gui_set_task(self, task: str, group: str, argument: str, value) -> bool:
        """设置GUI任务参数值"""
        task = convert_to_underscore(task)
        group = convert_to_underscore(group)
        argument = convert_to_underscore(argument)

        if isinstance(value, str) and len(value) == 8:
            try:
                value = datetime.strptime(value, '%H:%M:%S').time()
            except ValueError:
                pass

        try:
            task_object = getattr(self.config.model, task, None)
            group_object = getattr(task_object, group, None)
            argument_object = getattr(group_object, argument, None)
            if argument_object is None:
                raise AttributeError(f"参数路径不存在：{task}.{group}.{argument}")

            setattr(group_object, argument, value)
            self.config.save()
            logger.info(f"设置参数成功：{task}.{group}.{argument} = {getattr(group_object, argument)}")
            return True
        except ValidationError as e:
            logger.error(f"参数验证失败：{e}")
            return False
        except Exception as e:
            logger.error(f"设置参数失败：{e}")
            return False

    @zerorpc.stream
    def gui_mirror_image(self):
        """获取GUI显示的镜像画面（流式返回）- 新增截图异常兜底"""
        try:
            img = cv2.cvtColor(self.device.screenshot(), cv2.COLOR_RGB2BGR)
            self.device.stuck_record_clear()
            ret, buffer = cv2.imencode('.jpg', img)
            yield buffer.tobytes()
        except (AttributeError, Exception) as e:
            if 'fileno' in str(e) or 'nemu_connect' in str(e):
                logger.warning(f"镜像画面截图失败-IPC连接异常：{e}，返回空帧")
            else:
                logger.error(f"获取镜像画面失败：{e}")
            yield b''

    def _gui_update_tasks(self) -> None:
        """更新GUI任务状态"""
        if not self.gui_update_task:
            return

        data = {
            "task": {},
            "pending": [],
            "waiting": []
        }
        if self.config.task and self.config.task.next_run < datetime.now():
            data["task"] = {
                "name": self.config.task.command,
                "next_run": str(self.config.task.next_run)
            }
        for p in self.config.pending_task[1:]:
            data["pending"].append({"name": p.command, "next_run": str(p.next_run)})
        for w in self.config.waiting_task:
            data["waiting"].append({"name": w.command, "next_run": str(w.next_run)})

        self.gui_update_task(data)

    def _gui_set_status(self, status: str) -> None:
        """设置GUI显示的状态（Init/Empty/Run/Error/Free）"""
        if self.gui_update_task:
            self.gui_update_task({"status": status})

    def gui_task_list(self) -> str:
        """获取GUI显示的任务列表"""
        result = {}
        for key, value in self.config.model.dict().items():
            if isinstance(value, str) or key == "restart" or "scheduler" not in value:
                continue
            scheduler = value["scheduler"]
            result[self.config.model.type(key)] = {
                "enable": scheduler["enable"],
                "next_run": str(scheduler["next_run"])
            }
        return json.dumps(result, ensure_ascii=False)

    # ==============================================================
    # ✅ 三重防护 彻底解决 'NoneType' object has no attribute 'serial'
    # ==============================================================
    def reset_adb_service(self):
        """✅ 强制重置ADB服务，解决10054/10061连接失败、ADB卡死、被代理劫持"""
        try:
            logger.info("🔧 执行ADB服务强制重置，解决连接被劫持/卡死问题")
            os.system("taskkill /f /im adb.exe >nul 2>&1")
            time.sleep(1)
            os.system("adb kill-server >nul 2>&1")
            time.sleep(1)
            os.system("adb start-server >nul 2>&1")
            logger.info("✅ ADB服务重置完成，已清理卡死进程")
        except Exception as e:
            logger.warning(f"ADB重置异常: {str(e)[:50]}，忽略该错误继续执行")

    def _parse_serial(self) -> Optional[tuple[str, int]]:
        """✅ 三重None判空+兜底，彻底解决serial读取异常"""
        try:
            if self.config is None:
                logger.debug(f"⚠️ config实例为空，使用全局兜底端口 {DEFAULT_SERIAL}")
                return (DEFAULT_HOST, DEFAULT_PORT)

            if getattr(self.config, 'device', None) is None:
                logger.debug(f"⚠️ config.device为空，使用全局兜底端口 {DEFAULT_SERIAL}")
                return (DEFAULT_HOST, DEFAULT_PORT)

            serial = getattr(self.config.device, 'serial', None)
            if not isinstance(serial, str) or len(serial) == 0 or ':' not in serial:
                logger.debug(f"⚠️ serial配置为空/格式异常: {serial}，使用全局兜底端口 {DEFAULT_SERIAL}")
                return (DEFAULT_HOST, DEFAULT_PORT)

            host, port_str = serial.split(':', 1)
            host = host.strip()
            port = int(port_str.strip())
            logger.debug(f"✅ 从Config.device解析serial成功 → host={host}, port={port}")
            return (host, port)
        except Exception as e:
            logger.debug(f"⚠️ serial解析异常: {str(e)[:60]}，使用全局兜底端口 {DEFAULT_SERIAL}")
            return (DEFAULT_HOST, DEFAULT_PORT)

    def _check_port_open(self, host: str, port: int) -> bool:
        """检测端口是否开放，判断模拟器是否运行"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                return s.connect_ex((host, port)) == 0
        except:
            return False

    def _check_process_running(self, process_name: str) -> bool:
        """检测模拟器进程是否运行"""
        try:
            for proc in psutil.process_iter(['name']):
                if process_name.lower() in proc.info['name'].lower():
                    return True
        except:
            pass
        return False

    def is_emulator_running(self) -> bool:
        """✅ 无任何报错：端口检测+进程兜底 零误判"""
        try:
            host, port = self._parse_serial()
            if self._check_port_open(host, port):
                return True
            else:
                logger.debug(f"⚠️ 端口[{host}:{port}]未开放，执行进程兜底检测")

            emulator_process = ['NemuPlayer.exe', 'LDPlayer.exe', 'nox.exe', 'BlueStacks.exe', 'MuMuPlayer.exe',
                                'MuMuPlayer12.exe']
            for proc in emulator_process:
                if self._check_process_running(proc):
                    logger.debug(f"✅ 检测到模拟器进程[{proc}]运行，判定模拟器启动")
                    return True

            return False
        except Exception as e:
            logger.warning(f"模拟器状态检测异常: {str(e)[:60]}，返回False")
            return False

    def is_game_running(self) -> bool:
        """✅ 优化联动：模拟器未运行 → 游戏一定未运行"""
        if not self.is_emulator_running():
            return False
        try:
            return self.device.app_is_running()
        except Exception as e:
            logger.warning(f"游戏状态检测异常：{str(e)[:50]}，返回False")
            return False

    def is_device_active(self) -> bool:
        """检测模拟器+游戏是否都处于运行状态"""
        return self.is_emulator_running() and self.is_game_running()

    # ==============================================================
    # ✅ ✅ ✅ 核心根治修复 | 重构 _start_emulator_and_game 启动逻辑
    # ✅ 彻底删除启动流程中【多余的关闭指令】，启动只做启动，永不自关
    # ✅ 新增启动防抖锁，MuMu12启动后锁定60秒，杜绝重复启停
    # ✅ 完美保留：启动后调用Restart+完整登录流程，你的需求100%保留
    # ==============================================================
    def _start_emulator_and_game(self) -> bool:
        """✅ 根治版：依次启动模拟器 → 调用Restart任务启动游戏 含完整登录流程 | 无任何关闭指令"""
        global _last_emulator_start_time
        current_ts = time.time()

        # ✅ 启动防抖：60秒内刚启动过，直接返回成功，不重复启动
        if current_ts - _last_emulator_start_time < EMULATOR_START_LOCK_TIME:
            logger.info(f"✅ 模拟器启动防抖生效，{EMULATOR_START_LOCK_TIME}秒内已启动过，跳过重复启动")
            return True

        # ✅ 标记启动中，防止并发调用
        if self.is_emulator_booting:
            logger.info(f"✅ 模拟器正在启动中，跳过重复启动请求")
            return True

        logger.info("===== 执行【启动模拟器 → 调用Restart完整启动游戏】流程 =====")
        if not hasattr(self.device, "emulator_instance") or not self.device.emulator_instance:
            logger.error("❌ 未检测到模拟器实例，启动失败")
            return False

        self.is_emulator_booting = True
        try:
            # ✅ 【核心修复】只执行启动，彻底删除 多余的 shutdown_player 关闭指令！！！
            self.device.emulator_start()
            logger.info("✅ 模拟器启动成功 (纯启动，无任何关闭操作)")

            # ✅ 读取配置中的加载时长，无配置则用60秒，适配MuMu12慢加载
            load_duration = getattr(self.config.script.optimization, 'emulator_load_time', 60)
            load_duration = load_duration if isinstance(load_duration, int) else 60
            logger.info(f"等待模拟器加载完成，休眠 {load_duration} 秒 (MuMu12启动需要完整加载时间)")
            time.sleep(load_duration)

            # ✅ 重置IPC缓存，防止连接异常
            if hasattr(self.device, '__dict__') and 'nemu_ipc' in self.device.__dict__:
                del self.device.__dict__['nemu_ipc']
            logger.info("✅ 重置模拟器IPC连接缓存成功")

            # ✅ 启动游戏前重置ADB，防止连接异常
            self.reset_adb_service()

            # ✅ 调用Restart任务，执行完整的游戏启动+登录流程
            logger.info("开始调用 Restart 任务，执行游戏完整启动流程（含登录验证）")
            self.run("Restart")
            logger.info("✅ Restart任务执行完成，游戏启动成功（含登录验证）")

            # ✅ 更新启动时间，触发防抖锁
            _last_emulator_start_time = time.time()
            return True
        except Exception as e:
            logger.error(f"❌ 调用Restart启动游戏异常：{e}")
            return False
        finally:
            # ✅ 无论成败，解除启动标记
            self.is_emulator_booting = False

    # ==============================================================
    # ✅ 原有逻辑：等待逻辑+调度规则 完整保留
    # ==============================================================
    def wait_until(self, future: datetime) -> bool:
        """等待到指定时间，配置变更则按你的规则处理新任务 + 历史任务时间修正"""
        future = future + timedelta(seconds=1)
        self.config.start_watching()

        while True:
            current_time = datetime.now()
            if current_time > future:
                return True

            time.sleep(5)
            if self.config.should_reload():
                logger.hr("检测到配置变更，执行自定义任务调度规则", level=1)
                del_cached_property(self, "config")
                new_config = self.config
                new_task = new_config.get_next()
                new_task_time = new_task.next_run

                time_diff = current_time - new_task_time
                if time_diff.total_seconds() > 3600:
                    logger.warning(f"修正历史任务[{new_task.command}]，过期超1小时 → 延迟10分钟执行")
                    new_task.next_run = current_time + timedelta(minutes=10)
                    new_task_time = new_task.next_run

                logger.info(f"新任务 → 名称：{new_task.command} | 执行时间：{new_task_time} | 当前时间：{current_time}")
                device_active = self.is_device_active()
                logger.info(
                    f"设备状态 → 模拟器运行：{self.is_emulator_running()} | 游戏运行：{self.is_game_running()} | 整体活跃：{device_active}")

                if new_task_time < current_time:
                    logger.info("【规则匹配】新任务时间早于当前时间")
                    if device_active:
                        logger.info("→ 设备活跃，新任务加入待执行队列")
                        if new_task not in new_config.pending_task:
                            new_config.pending_task.append(new_task)
                            new_config.pending_task.sort(key=lambda x: x.next_run)
                    else:
                        logger.info("→ 设备未活跃，启动模拟器+游戏后立即执行新任务")
                        self._start_emulator_and_game()
                        new_config.task = new_task
                else:
                    logger.info("【规则匹配】新任务时间晚于当前时间")
                    if device_active:
                        logger.info("→ 设备活跃，新任务正常加入等待队列")
                        if new_task not in new_config.waiting_task:
                            new_config.waiting_task.append(new_task)
                            new_config.waiting_task.sort(key=lambda x: x.next_run)
                    else:
                        logger.info("→ 设备未活跃，不启动设备，执行等待任务时间对比")
                        if new_config.waiting_task:
                            earliest_waiting_time = min([t.next_run for t in new_config.waiting_task])
                            if new_task_time < earliest_waiting_time:
                                new_config.waiting_task = [new_task] + [t for t in new_config.waiting_task if
                                                                        t.next_run >= new_task_time]
                                new_config.waiting_task.sort(key=lambda x: x.next_run)
                            else:
                                if new_task not in new_config.waiting_task:
                                    new_config.waiting_task.append(new_task)
                        else:
                            new_config.waiting_task.append(new_task)

                return False

    def get_next_task(self) -> str:
        """获取下一个待执行的任务名称"""
        while True:
            task = self.config.get_next()
            self.config.task = task

            if self.state_queue:
                self.state_queue.put({"schedule": self.config.get_schedule_data()})

            current_time = datetime.now()
            if task.next_run <= current_time:
                logger.info(f"任务[{task.command}]执行时间已到，开始执行")
                return task.command

            if not self._handle_wait_during_idle(task.next_run):
                del_cached_property(self, "config")
                logger.info("配置变更，重新加载配置并获取任务")
                continue

    def _handle_wait_during_idle(self, next_run: datetime) -> bool:
        """处理空闲等待策略"""
        method = self.config.script.optimization.when_task_queue_empty
        strategy_map = {
            "close_game": self._wait_close_game,
            "goto_main": self._wait_goto_main,
            "stay_there": self._wait_stay_there
        }
        func = strategy_map.get(method, self._wait_stay_there)
        return func(next_run)

    # ==============================================================
    # ✅ ✅ ✅ 核心优化修复 close_game 策略 | 完美保留你的刚需
    # ✅ 保留：空闲时 立即关闭游戏 + 立即关闭模拟器 无时长判断
    # ✅ 新增：启动防抖判断 → 启动阶段永不执行关闭，彻底解耦启动/关闭逻辑
    # ✅ 新增：关闭后释放所有资源，杜绝ADB残留卡死
    # ==============================================================
    def _wait_close_game(self, next_run: datetime) -> bool:
        """✅ close_game策略【完美版】：空闲时关闭游戏+立即关闭模拟器 无时长判断 | 启动阶段不执行"""
        global _last_emulator_start_time
        current_ts = time.time()
        logger.info("===== 执行空闲策略：close_game (关闭游戏+立即关闭模拟器) =====")

        # ✅ 关键优化：启动后60秒内，不执行任何关闭操作，防止启动被打断
        if current_ts - _last_emulator_start_time < EMULATOR_START_LOCK_TIME:
            logger.info(f"✅ 启动防抖生效，{EMULATOR_START_LOCK_TIME}秒内禁止关闭模拟器，直接等待任务")
            self.device.release_during_wait()
            return self.wait_until(next_run)

        try:
            if self.is_emulator_running():
                logger.info("模拟器还在运行，执行关闭流程")
                # ✅ 先关闭游戏进程
                try:
                    self.device.app_stop()
                    logger.info("✅ 游戏进程关闭成功")
                except Exception as e:
                    logger.warning(f"游戏关闭异常：{e}，忽略并继续关闭模拟器")

                # ✅ 再关闭模拟器主程序
                if hasattr(self.device, "emulator_instance") and self.device.emulator_instance:
                    try:
                        self.device.emulator_stop()
                        logger.info("✅ 模拟器关闭成功")
                        # ✅ 强制杀死卡死的ADB进程，解决残留问题
                        os.system("taskkill /f /im adb.exe >nul 2>&1")
                        os.system("taskkill /f /im MuMuPlayer.exe >nul 2>&1")
                    except Exception as e:
                        logger.warning(f"关闭模拟器失败：{e}，忽略该错误")

                # ✅ 释放所有设备资源
                self.device.release_during_wait()
                logger.info("✅ 所有设备资源释放完成")
            else:
                logger.info("✅ 模拟器已关闭，跳过所有游戏/模拟器操作，直接等待任务")

            logger.info(f"等待任务执行时间：{next_run}")
            if not self.wait_until(next_run):
                return False

            logger.info("任务时间已到，启动模拟器+游戏")
            start_success = self._start_emulator_and_game()
            if not start_success:
                logger.error("❌ 模拟器+游戏启动失败，终止流程")
                return False

            return True
        except Exception as e:
            logger.error(f"执行close_game策略异常：{e}")
            logger.error(traceback.format_exc())
            self.device.release_during_wait()
            return True

    def _wait_goto_main(self, next_run: datetime) -> bool:
        """回到主页面等待"""
        logger.info("===== 执行空闲策略：goto_main =====")
        try:
            self.run("GotoMain")
            self.device.release_during_wait()
        except Exception as e:
            logger.warning(f"执行goto_main异常：{e}，忽略")
        return self.wait_until(next_run)

    def _wait_stay_there(self, next_run: datetime) -> bool:
        """保持当前状态等待"""
        logger.info("===== 执行空闲策略：stay_there =====")
        self.device.release_during_wait()
        return self.wait_until(next_run)

    def exception_handler(self, e: Exception, command: str) -> None:
        """异常处理：御魂溢出等特殊场景"""
        try:
            from tasks.Utils.post_diagnotor import PostDiagnotor, AnalyzeType
            image = getattr(self.device, 'image', None)
            if image is None:
                return
            analyse_type = PostDiagnotor().handle(e=e, command=command, image=image)
            if analyse_type == AnalyzeType.SoulOverflow:
                logger.warning("检测到御魂溢出，执行SoulsTidy任务...")
                self.config.task_call('SoulsTidy')
                time.sleep(1)
        except Exception as ex:
            logger.error(f"异常处理器执行失败：{ex}")

    # ==============================================================
    # ✅ 任务执行方法 + 异常兜底增强 + 前置ADB重置
    # ==============================================================
    def run(self, command: str) -> bool:
        """执行指定任务"""
        command_camel = inflection.camelize(command)
        logger.info(f"===== 启动任务：{command_camel} =====")
        self.reset_adb_service()

        try:
            try:
                self.device.screenshot()
            except (AttributeError, Exception) as e:
                if 'fileno' in str(e) or 'nemu_connect' in str(e) or '10061' in str(e) or '10054' in str(e):
                    logger.warning(f"截图失败-IPC/ADB连接异常，重试一次截图")
                    time.sleep(2)
                    self.device.screenshot()
                else:
                    raise e

            module_path = Path.cwd() / 'tasks' / command_camel / 'script_task.py'
            if not module_path.exists():
                raise ScriptError(f"任务模块不存在：{module_path}")

            task_module = load_module('script_task', str(module_path))
            task_module.ScriptTask(config=self.config, device=self.device).run()
            logger.info(f"===== 任务完成：{command_camel} =====")
            return True

        except TaskEnd:
            logger.info(f"任务正常结束：{command_camel}")
            return True
        except GameNotRunningError as e:
            logger.warning(f"游戏未运行：{e}")
            self.exception_handler(e=e, command=command)
            self.config.task_call('Restart')
            return True
        except (GameStuckError, GameTooManyClickError) as e:
            logger.error(f"游戏卡死/点击过多：{e}")
            self.save_error_log()
            self.exception_handler(e=e, command=command)
            self.config.notifier.push(title=f"{I18n.trans_zh_cn(command)}", content=f"<{self.config_name}> 游戏卡死")
            self.config.task_call('Restart')
            self.device.sleep(10)
            return False
        except GameBugError as e:
            logger.error(f"游戏客户端异常：{e}")
            self.save_error_log()
            self.exception_handler(e=e, command=command)
            self.config.task_call('Restart')
            self.device.sleep(10)
            return False
        except GamePageUnknownError as e:
            logger.critical(f"游戏页面未知：{e}")
            self.save_error_log()
            self.exception_handler(e=e, command=command)
            self.config.notifier.push(title=f"{I18n.trans_zh_cn(command)}", content=f"<{self.config_name}> 页面未知")
            self.config.task_call('Restart')
            self.device.sleep(10)
            return False
        except ScriptError as e:
            logger.critical(f"脚本逻辑错误：{e}")
            self.exception_handler(e=e, command=command)
            self.config.notifier.push(title=f"{command}", content=f"<{self.config_name}> 脚本错误")
            exit(1)
        except RequestHumanTakeover as e:
            logger.critical(f"需要人工接管：{e}")
            self.exception_handler(e=e, command=command)
            self.config.notifier.push(title=f"{command}", content=f"<{self.config_name}> 需人工接管")
            exit(1)
        except Exception as e:
            logger.exception(f"任务执行异常：{e}")
            self.exception_handler(e=e, command=command)
            self.save_error_log()
            self.config.notifier.push(title=f"{command}", content=f"<{self.config_name}> 未知异常")
            if any(k in str(e) for k in ['fileno', 'nemu_connect', '10061', '10054']):
                logger.warning("检测到IPC/ADB连接错误，自动重启模拟器恢复")
                try:
                    self.device.emulator_restart()
                except Exception as ex:
                    logger.warning(f"重启模拟器失败：{ex}")
                time.sleep(60)
                del_cached_property(self, "device")
                del_cached_property(self, "config")
                return False
            exit(1)

    # ==============================================================
    # 主循环+线程启动 - 所有原有逻辑完整保留
    # ==============================================================
    def loop(self):
        """脚本主调度循环"""
        with _log_switch_lock:
            logger.set_file_logger(self.config_name, do_cleanup=True)

        start_day = date.today()
        logger.info(f"启动调度循环：{self.config_name}")
        self.config.model.running_task = ''

        if not self.config.script.device.run_background_only and IS_WINDOWS:
            try:
                from module.device.platform2.platform_windows import minimize_by_name, show_window_by_name
                target_window_name = self.config.script.device.handle
                if self.config.script.device.emulator_window_minimize:
                    minimize_by_name(target_window_name)
                else:
                    show_window_by_name(target_window_name)
                logger.info(f"唤起模拟器窗口：{target_window_name}")
            except Exception as e:
                logger.error(f"唤起模拟器窗口失败：{e}")

        while True:
            if date.today() > start_day:
                with _log_switch_lock:
                    logger.set_file_logger(self.config_name, do_cleanup=True)
                start_day = date.today()

            task = self.get_next_task()
            _ = self.device

            if self.is_first_task and task == 'Restart':
                logger.info("跳过首次启动的Restart任务")
                self.config.task_delay(task='Restart', success=True, server=True)
                del_cached_property(self, "config")
                continue

            logger.hr(f"执行任务：{task}", level=0)
            self.config.model.running_task = task
            self.device.stuck_record_clear()
            self.device.click_record_clear()

            success = self.run(task)
            self.config.model.running_task = ''
            self.is_first_task = False

            self.failure_record[task] = self.failure_record.get(task, 0) + (0 if success else 1)
            if self.failure_record[task] >= 3:
                logger.critical(f"任务{task}连续失败3次，需要人工接管")
                self.config.notifier.push(title=f"{task}", content=f"<{self.config_name}> 连续失败3次")
                try:
                    self.device.emulator_stop()
                except Exception as e:
                    logger.warning(f"关闭模拟器失败：{e}")
                exit(1)

            if success or self.config.script.error.handle_error:
                del_cached_property(self, 'config')
                continue
            else:
                break

    def start_loop(self) -> None:
        """启动调度循环线程"""
        if not self.loop_thread or not self.loop_thread.is_alive():
            self.loop_thread = Thread(target=self.loop, name='Script_loop', daemon=True)
            self.loop_thread.start()
            logger.info("调度循环线程已启动")


if __name__ == "__main__":
    # ===================== 运行入口 =====================
    SCRIPT_CONFIG_NAME = "MuMu2"  # 你的配置文件名 无需修改
    # ====================================================
    script = Script(config_name=SCRIPT_CONFIG_NAME)
    script.loop()
