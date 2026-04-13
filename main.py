import logging
import os
import random
import sys
import time
import tempfile
import shutil
import platform  # 新增，用于检测操作系统
from dataclasses import dataclass
from typing import List, Dict

import ddddocr
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from config import CONFIG
from account_parser import parse_accounts, Account
from api_client import RainyunAPI
from server_manager import ServerManager

logger = logging.getLogger(__name__)


@dataclass
class AccountResult:
    """单个账号执行结果"""
    username: str
    login_success: bool = False
    sign_in_success: bool = False
    points_before: int = 0
    points_after: int = 0
    points_earned: int = 0
    auto_renew_enabled: bool = False
    renew_summary: str = ""
    error_msg: str = ""
    
    def is_success(self) -> bool:
        """是否成功"""
        return self.login_success and self.sign_in_success


@dataclass
class RuntimeContext:
    """运行时上下文"""
    driver: webdriver.Chrome
    wait: WebDriverWait
    ocr: ddddocr.DdddOcr
    det: ddddocr.DdddOcr
    temp_dir: str
    config: dict
    
    def temp_path(self, filename: str) -> str:
        """获取临时文件路径"""
        return os.path.join(self.temp_dir, filename)


def init_logger():
    """初始化日志"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    logger.info("=" * 80)
    logger.info("雨云签到工具 by SerendipityR ~")
    logger.info("Github发布页: https://github.com/SerendipityR-2022/Rainyun-Qiandao")
    logger.info("-" * 80)
    logger.info("雨云签到工具容器版 by fatekey ~")
    logger.info("Github发布页: https://github.com/fatekey/Rainyun-Qiandao")
    logger.info("-" * 80)
    logger.info("                   项目为二次开发青龙脚本化运行")
    logger.info("                     本项目基于上述项目开发")
    logger.info("                本项目仅作为学习参考，请勿用于其他用途")
    logger.info("=" * 80)


def init_selenium(config: dict):
    """初始化 Selenium 驱动（支持 Windows 和 Linux）"""
    logger.info("🔧 开始初始化 Selenium WebDriver")
    
    ops = Options()
    # 基础配置
    ops.add_argument("--no-sandbox")
    ops.add_argument("--disable-dev-shm-usage")
    ops.add_argument("--headless=new")  # 无头模式，Windows 下也支持
    ops.add_argument("--disable-gpu")
    ops.add_argument("--window-size=1920,1080")
    logger.info("   - 已配置无沙盒模式")
    logger.info("   - 已配置无头模式")
    logger.info("   - 已配置窗口尺寸: 1920x1080")
    
    # User-Agent
    ops.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    logger.info("   - 已设置 User-Agent")
    
    # 反爬配置
    ops.add_experimental_option("excludeSwitches", ["enable-automation"])
    ops.add_experimental_option('useAutomationExtension', False)
    ops.add_argument("--disable-blink-features=AutomationControlled")
    logger.info("   - 已启用反自动化检测配置")
    
    # 根据操作系统选择 ChromeDriver 路径
    system = platform.system()
    if system == "Windows":
        # Windows 下假设 chromedriver.exe 在系统 PATH 中，或直接使用 "chromedriver.exe"
        driver_path = "chromedriver.exe"
        logger.info(f"   - Windows 系统，使用 chromedriver.exe（需在 PATH 中）")
    elif system == "Linux":
        driver_path = "/usr/bin/chromedriver"
        logger.info(f"   - Linux 系统，使用路径: {driver_path}")
    else:
        # macOS 或其他系统
        driver_path = "chromedriver"
        logger.info(f"   - 其他系统，使用 chromedriver（需在 PATH 中）")
    
    # 检查文件是否存在（Windows 下不强制检查 PATH 中的文件，因为 os.path.exists 可能找不到）
    if system != "Windows" and not os.path.exists(driver_path):
        logger.error(f"❌ 未找到 chromedriver: {driver_path}")
        if system == "Linux":
            logger.error("请在青龙终端执行：apt update && apt install -y chromium-driver")
        raise FileNotFoundError(f"chromedriver not found at {driver_path}")
    
    try:
        service = Service(executable_path=driver_path)
        driver = webdriver.Chrome(service=service, options=ops)
        driver.delete_all_cookies()
        logger.info("✅ Selenium WebDriver 初始化成功")
        return driver
    except Exception as e:
        logger.error(f"❌ Selenium 初始化失败: {e}")
        if system == "Windows":
            logger.error("请确保已安装 Chrome 浏览器并下载对应版本的 chromedriver.exe")
            logger.error("下载地址: https://chromedriver.chromium.org/downloads")
        raise


def inject_stealth_js(driver, config: dict):
    """注入反检测脚本（支持相对路径）"""
    # 获取主脚本所在目录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 从配置读取相对路径
    relative_path = config.get("stealth_js_path", "../stealth.min.js")
    
    # 拼接完整路径
    script_path = os.path.join(script_dir, relative_path)
    script_path = os.path.abspath(script_path)  # 转为绝对路径
    
    logger.info(f"🔧 检查反检测脚本: {script_path}")
    
    if not os.path.exists(script_path):
        logger.error(f"❌ 未找到 stealth.min.js！")
        logger.error(f"预期路径: {script_path}")
        logger.error(f"主脚本目录: {script_dir}")
        logger.error(f"配置的相对路径: {relative_path}")
        logger.error("请检查以下几点：")
        logger.error("  1. 文件是否已上传")
        logger.error("  2. 文件名是否正确（区分大小写）")
        logger.error("  3. 配置的相对路径是否正确")
        logger.error("下载地址: https://raw.githubusercontent.com/berstend/puppeteer-extra/master/packages/puppeteer-extra-plugin-stealth/evasions/stealth.min.js")
        sys.exit(1)
    
    with open(script_path, "r", encoding="utf-8") as f:
        js = f.read()
    
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": js})
    logger.info("✅ 已注入 stealth.min.js 反检测脚本")


def do_login(ctx: RuntimeContext, username: str, password: str) -> bool:
    """执行登录"""
    try:
        logger.info("=" * 60)
        logger.info("⏳ 发起登录请求")
        logger.info("🌐 访问雨云登录页: https://app.rainyun.com/auth/login")
        ctx.driver.get("https://app.rainyun.com/auth/login")
        
        logger.info(f"   当前页面标题: {ctx.driver.title}")
        logger.info(f"   当前页面URL: {ctx.driver.current_url}")
        
        logger.info("⏳ 等待登录表单元素加载...")
        username_elem = ctx.wait.until(EC.visibility_of_element_located((By.NAME, "login-field")))
        password_elem = ctx.wait.until(EC.visibility_of_element_located((By.NAME, "login-password")))
        login_btn = ctx.wait.until(EC.element_to_be_clickable((By.XPATH, "//button[@type='submit' and contains(., '登')]")))
        
        logger.info("✅ 登录表单元素加载完成")
        logger.info("📝 输入账号密码")
        username_elem.send_keys(username)
        password_elem.send_keys(password)
        
        logger.info("🖱️  点击登录按钮")
        login_btn.click()
        logger.info("⏳ 正在登录中，耗时较长请稍等……")
        time.sleep(3)
        
        # 处理登录验证码
        try:
            logger.info("🔍 检查是否触发登录验证码...")
            ctx.wait.until(EC.visibility_of_element_located((By.ID, "tcaptcha_iframe_dy")))
            logger.warning("⚠️  触发登录验证码！")
            ctx.driver.switch_to.frame("tcaptcha_iframe_dy")
            
            from captcha import process_captcha
            if not process_captcha(ctx, ctx.config):
                logger.error("❌ 登录验证码处理失败")
                return False
                
            logger.info("✅ 登录验证码处理成功")
        except TimeoutException:
            logger.info("✅ 未触发登录验证码")
        
        ctx.driver.switch_to.default_content()
        logger.info("⏳ 等待页面跳转...")
        time.sleep(5)
        
        # 验证登录状态
        current_url = ctx.driver.current_url
        logger.info(f"   跳转后URL: {current_url}")
        logger.info(f"   当前页面标题: {ctx.driver.title}")
        
        if "dashboard" not in current_url:
            logger.error(f"❌ 登录失败！未跳转到控制台页面")
            logger.error(f"   当前URL: {current_url}")
            return False
        
        # 获取用户名
        try:
            user_elem = ctx.driver.find_element(By.XPATH, '//*[@id="app"]/div[1]/nav/div[1]/ul/div[6]/li/a/div/div/p')
            user_name = user_elem.text.strip()
            logger.info(f"✅ 账号登录成功: {user_name}")
        except Exception:
            logger.info("✅ 登录成功！")
        
        return True
        
    except TimeoutException:
        logger.error("❌ 页面加载超时！")
        logger.error("   可能原因：")
        logger.error("   1. 网络连接问题")
        logger.error("   2. 页面加载时间过长，请尝试增加 timeout 配置")
        logger.error("   3. 雨云服务器响应慢")
        return False
    except Exception as e:
        logger.error(f"❌ 登录异常: {e}", exc_info=True)
        return False


def do_sign_in(ctx: RuntimeContext) -> bool:
    """执行签到"""
    try:
        logger.info("=" * 60)
        logger.info("🌐 访问赚取积分页: https://app.rainyun.com/account/reward/earn")
        ctx.driver.get("https://app.rainyun.com/account/reward/earn")
        ctx.driver.implicitly_wait(5)
        
        logger.info(f"   当前页面URL: {ctx.driver.current_url}")
        logger.info(f"   当前页面标题: {ctx.driver.title}")
        
        # 查找签到按钮
        logger.info("🔍 查找每日签到按钮...")
        try:
            earn_btn_qddiv = ctx.driver.find_element(By.XPATH, '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[2]/div/div/div/div[1]/div')
            earn_btn_qd = earn_btn_qddiv.find_element(By.XPATH, './/span[contains(text(),"每日签到")]')
            status_elem = earn_btn_qd.find_element(By.XPATH, './following-sibling::span[1]')
            status_text = status_elem.text.strip()
            
            logger.info(f"📌 签到状态: {status_text}")
            
            if status_text == "领取奖励":
                earn_btn = status_elem.find_element(By.XPATH, './a')
                logger.info("🎯 开始领取签到奖励")
                earn_btn.click()
                
                # 处理签到验证码
                time.sleep(2)
                logger.info("⚠️  触发签到验证码")
                ctx.driver.switch_to.frame("tcaptcha_iframe_dy")
                
                from captcha import process_captcha
                if not process_captcha(ctx, ctx.config):
                    logger.error("❌ 签到验证码处理失败")
                    return False
                
                ctx.driver.switch_to.default_content()
                logger.info("⏳ 等待签到结果...")
                time.sleep(5)
                
                logger.info("✅ 签到奖励领取成功")
            else:
                logger.info(f"📌 {status_text}，无需重复签到")
            
            # 获取当前积分
            try:
                points_elem = ctx.driver.find_element(By.XPATH, '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[1]/div[1]/div/p/div/h3')
                import re
                current_points = int(''.join(re.findall(r'\d+', points_elem.text)))
                logger.info(f"💰 当前积分: {current_points} （约 {current_points/2000:.2f} 元）")
            except Exception as e:
                logger.warning(f"⚠️  积分获取失败: {e}")
            
            return True
            
        except TimeoutException:
            logger.error("❌ 未找到签到按钮")
            return False
        
    except Exception as e:
        logger.error(f"❌ 签到异常: {e}", exc_info=True)
        return False


def execute_auto_renew(account: Account, config: dict) -> str:
    """
    执行自动续费
    
    Returns:
        续费结果摘要
    """
    logger.info("=" * 60)
    logger.info("🔄 开始执行自动续费检查")
    try:
        api = RainyunAPI(account.api_key, config)
        manager = ServerManager(api, config)
        
        result = manager.check_and_renew()
        report = manager.generate_report(result)
        
        logger.info("\n" + report)
        
        # 生成简短摘要
        summary = f"续费: {result['renewed']}台成功, {result['skipped']}台跳过, {result['failed']}台失败"
        return summary
        
    except Exception as e:
        logger.error(f"❌ 自动续费失败: {e}", exc_info=True)
        return f"续费失败: {str(e)}"


def sign_in_rainyun(account: Account, config: dict) -> AccountResult:
    """
    单账号签到流程
    
    Returns:
        账号执行结果
    """
    result = AccountResult(username=account.username)
    driver = None
    temp_dir = None
    
    try:
        logger.info("\n" + "=" * 80)
        logger.info(f"开始处理账号: {account.username}")
        logger.info("=" * 80)
        
        # 随机延时
        delay_min = random.randint(0, config["max_delay"])
        delay_sec = random.randint(0, 60)
        logger.info(f"⏳ 随机延时 {delay_min} 分钟 {delay_sec} 秒")
        time.sleep(delay_min * 60 + delay_sec)
        
        # 初始化组件
        logger.info("🔧 初始化 ddddocr 验证码识别库")
        ocr = ddddocr.DdddOcr(ocr=True, show_ad=False)
        det = ddddocr.DdddOcr(det=True, show_ad=False)
        logger.info("✅ ddddocr 初始化成功")
        
        driver = init_selenium(config)
        inject_stealth_js(driver, config)
        wait = WebDriverWait(driver, config["timeout"])
        
        # 创建临时目录
        temp_dir = tempfile.mkdtemp(prefix="rainyun-")
        logger.info(f"📁 临时目录: {temp_dir}")
        
        # 构建上下文
        ctx = RuntimeContext(
            driver=driver,
            wait=wait,
            ocr=ocr,
            det=det,
            temp_dir=temp_dir,
            config=config
        )
        
        # 记录签到前积分
        if account.api_key:
            try:
                logger.info("🔍 正在获取签到前积分...")
                api = RainyunAPI(account.api_key, config)
                result.points_before = api.get_user_points()
                logger.info(f"💰 签到前积分: {result.points_before} （约 {result.points_before / config['points_to_cny_rate']:.2f} 元）")
            except Exception as e:
                logger.warning(f"⚠️  获取初始积分失败: {e}")
        
        # 执行登录
        result.login_success = do_login(ctx, account.username, account.password)
        if not result.login_success:
            result.error_msg = "登录失败"
            logger.error("❌ 登录失败，跳过该账号")
            return result
        
        # 执行签到
        result.sign_in_success = do_sign_in(ctx)
        if not result.sign_in_success:
            result.error_msg = "签到失败"
            logger.error("❌ 签到失败")
            return result
        
        # 记录签到后积分
        if account.api_key:
            try:
                logger.info("🔍 正在获取签到后积分...")
                api = RainyunAPI(account.api_key, config)
                result.points_after = api.get_user_points()
                result.points_earned = result.points_after - result.points_before
                logger.info(f"💰 当前积分: {result.points_after} (本次获得 {result.points_earned} 分)")
                logger.info(f"💵 约合人民币: {result.points_after / config['points_to_cny_rate']:.2f} 元")
            except Exception as e:
                logger.warning(f"⚠️  获取最终积分失败: {e}")
        
        # 执行自动续费（如果启用）
        result.auto_renew_enabled = account.auto_renew
        if account.auto_renew and account.api_key:
            result.renew_summary = execute_auto_renew(account, config)
        elif account.auto_renew and not account.api_key:
            result.renew_summary = "未配置API Key，跳过续费"
            logger.warning("⚠️  该账号已启用自动续费但未配置 API Key，跳过续费")
        
        logger.info(f"✅ 账号 {account.username} 处理完成")
        return result
        
    except Exception as e:
        result.error_msg = f"异常: {str(e)}"
        logger.error(f"❌ 账号处理异常: {e}", exc_info=True)
        return result
        
    finally:
        # 清理资源
        if driver:
            try:
                driver.quit()
                logger.info(f"🔒 浏览器已关闭")
            except Exception as e:
                logger.warning(f"⚠️  关闭浏览器失败: {e}")
        
        if temp_dir:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.info(f"🗑️  临时文件已清理")
            except Exception as e:
                logger.warning(f"⚠️  清理临时文件失败: {e}")
        
        logger.info("=" * 80 + "\n")


def generate_summary_report(results: List[AccountResult], config: dict) -> str:
    """
    生成汇总报告
    
    Args:
        results: 所有账号的执行结果
        config: 配置字典
        
    Returns:
        汇总报告文本
    """
    lines = []
    lines.append("=" * 60)
    lines.append("📊 雨云签到任务执行报告")
    lines.append("=" * 60)
    
    # 统计信息
    total = len(results)
    success = sum(1 for r in results if r.is_success())
    failed = total - success
    
    lines.append(f"\n📈 总体统计:")
    lines.append(f"  总账号数: {total}")
    lines.append(f"  ✅ 成功: {success}")
    lines.append(f"  ❌ 失败: {failed}")
    
    # 积分统计
    total_points_before = sum(r.points_before for r in results)
    total_points_after = sum(r.points_after for r in results)
    total_earned = sum(r.points_earned for r in results)
    
    if total_points_after > 0:
        lines.append(f"\n💰 积分统计:")
        lines.append(f"  签到前总积分: {total_points_before}")
        lines.append(f"  签到后总积分: {total_points_after}")
        lines.append(f"  本次获得: {total_earned} 分")
        lines.append(f"  约合人民币: {total_points_after / config['points_to_cny_rate']:.2f} 元")
    
    # 各账号详情
    lines.append(f"\n📋 各账号详情:")
    lines.append("-" * 60)
    
    for idx, result in enumerate(results, 1):
        lines.append(f"\n【账号 {idx}】 {result.username}")
        
        if result.is_success():
            lines.append(f"  状态: ✅ 成功")
            if result.points_after > 0:
                lines.append(f"  积分: {result.points_before} → {result.points_after} (+{result.points_earned})")
            if result.auto_renew_enabled:
                lines.append(f"  自动续费: ✅ 已启用")
                if result.renew_summary:
                    lines.append(f"    {result.renew_summary}")
            else:
                lines.append(f"  自动续费: ⏭️  未启用")
        else:
            lines.append(f"  状态: ❌ 失败")
            lines.append(f"  原因: {result.error_msg}")
    
    lines.append("\n" + "=" * 60)
    lines.append(f"📅 执行时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 60)
    
    return "\n".join(lines)


def send_notification(title: str, content: str):
    """
    发送通知（兼容青龙面板，Windows 下仅输出日志）
    
    Args:
        title: 通知标题
        content: 通知内容
    """
    try:
        # 尝试导入青龙面板的 notify 模块
        try:
            from notify import send
            send(title, content)
            logger.info("✅ 通知已发送（青龙面板 notify）")
            return
        except ImportError:
            # 青龙旧版可能使用 QLAPI
            try:
                print(QLAPI.notify(title, content))
                logger.info("✅ 通知已发送（青龙面板 QLAPI）")
                return
            except NameError:
                pass
        
        # 如果没有配置通知，仅在日志中输出
        logger.info("=" * 60)
        logger.info("📬 执行结果通知:")
        logger.info("-" * 60)
        logger.info(content)
        logger.info("=" * 60)
        logger.info("💡 提示: 如需推送通知，请在青龙面板配置通知渠道或使用其他通知方式")
        
    except Exception as e:
        logger.warning(f"⚠️  发送通知失败: {e}")


def main():
    """主函数"""
    # 记录开始时间
    start_time = time.time()
    
    init_logger()
    
    # 加载配置
    config = CONFIG.config
    
    # 解析账号
    accounts = parse_accounts()
    
    # 存储所有账号的执行结果
    all_results: List[AccountResult] = []
    
    # 依次处理每个账号
    for idx, account in enumerate(accounts, 1):
        logger.info(f"\n{'#'*80}")
        logger.info(f"第 {idx}/{len(accounts)} 个账号")
        logger.info(f"{'#'*80}")
        
        try:
            result = sign_in_rainyun(account, config)
            all_results.append(result)
        except Exception as e:
            logger.error(f"账号 {account.username} 处理失败: {e}")
            # 即使失败也要记录结果
            failed_result = AccountResult(
                username=account.username,
                error_msg=f"未知异常: {str(e)}"
            )
            all_results.append(failed_result)
        
        # 账号间间隔
        if idx < len(accounts):
            interval = random.uniform(3, 6)
            logger.info(f"⏳ 等待 {interval:.1f} 秒后处理下一个账号...")
            time.sleep(interval)
    
    # 计算总耗时
    elapsed_time = time.time() - start_time
    minutes = int(elapsed_time // 60)
    seconds = int(elapsed_time % 60)
    
    # 生成汇总报告
    logger.info("\n" + "=" * 80)
    logger.info("🎉 所有账号处理完成！")
    logger.info(f"⏱️  总耗时: {minutes} 分钟 {seconds} 秒")
    logger.info("=" * 80)
    
    # 生成并发送通知
    summary_report = generate_summary_report(all_results, config)
    logger.info("\n" + summary_report)
    
    # 发送通知
    send_notification("雨云签到任务完成", summary_report)


if __name__ == "__main__":
    main()