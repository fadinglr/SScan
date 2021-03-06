#!/usr/bin/python3
# -*- coding:utf-8 -*-
# @Author : yhy

import asyncio
import ipaddress
from config.log import logger
from urllib.parse import urlparse
import socket
from lib.common.scanner import Scanner
from lib.modules.iscdn import check_cdn
from config.setting import fofaApi
from lib.modules.fofa import fmain

# 进度条设置
from rich.progress import (
    BarColumn,
    TimeRemainingColumn,
    Progress,
)

# 扫描进程
def scan_process(targets):
    target, q_results, args = targets[0], targets[1], targets[2]
    scanner = Scanner(args=args)
    try:
        '''
        {'scheme': 'http', 'host': '47.97.164.40', 'port': 80, 'path': '', 'ports_open': [80], 'is_neighbor': 0}
        '''
        # 处理目标信息，加载规则，脚本等等
        ret = scanner.init_from_url(target)
        if ret:
            host, results = scanner.scan()
            if results:
                q_results.put((host, results))

    except Exception as e:
        logger.log('ERROR', f'{str(e)}')
    finally:
        return target


# 检测端口是否开放
async def port_scan_check(ip_port, semaphore, progress, task):
    async with semaphore:
        ip, port = ip_port[0], ip_port[1]
        conn = asyncio.open_connection(ip, port)
        try:
            reader, writer = await asyncio.wait_for(conn, timeout=10)
            # print(ip, port, 'open', ip_port[2], ip_port[3], ip_port[4])
            # 127.0.0.1 3306 open http /test.html 8080
            conn.close()
            progress.update(task, advance=1)
            return ip, port, 'open', ip_port[2], ip_port[3], ip_port[4]
        except Exception as e:
            conn.close()
            progress.update(task, advance=1)
            return ip, port, 'close', ip_port[2], ip_port[3], ip_port[4]


# 对给定目标进行80、443、指定的端口、脚本需要的端口进行探测，
def get_ip_port_list(queue_targets, args):
    ip_port_list = []
    for _target in queue_targets:
        url = _target
        # scheme netloc path
        if url.find('://') < 0:
            scheme = 'unknown'
            netloc = url[:url.find('/')] if url.find('/') > 0 else url
            path = ''
        else:
            # scheme='http', netloc='www.baidu.com:80', path='', params='', query='', fragment=''
            scheme, netloc, path, params, query, fragment = urlparse(url, 'http')

        # 指定端口时需要，检查指定的端口是否开放
        if netloc.find(':') >= 0:
            _ = netloc.split(':')
            host = _[0]
            port = int(_[1])
        else:
            host = netloc
            port = None

        if scheme == 'https' and port is None:
            port = 443
        elif scheme == 'http' and port is None:
            port = 80

        if scheme == 'unknown':
            if port == 80:
                scheme = 'http'
            elif port == 443:
                scheme = 'https'

        if port:            # url中指定了协议或端口
            ip_port_list.append((host, port, scheme, path, port))
        else:               # url中没指定扫描80，443
            port = 80
            ip_port_list.append((host, 80, scheme, path, 80))
            ip_port_list.append((host, 443, scheme, path, 443))

        # 没有禁用插件时，把插件中需要扫描的端口加进去
        if not args.no_scripts:
            for s_port in args.require_ports:
                ip_port_list.append((host, s_port, scheme, path, port))

    return list(set(ip_port_list))


# 对目标进行封装，格式化
# {'127.0.0.1': {'scheme': 'http', 'host': '127.0.0.1', 'port': 80, 'path': '', 'ports_open': [80, 3306], 's_port': -1}
def get_target(tasks, fofa_result):
    targets = {}
    for task in tasks:
        if task.result()[2] == 'open':
            host = task.result()[0]
            scheme = task.result()[3]
            path = task.result()[4]
            if host in targets:
                port = targets[host]['ports_open']
                port.append(task.result()[1])
                targets[host].update(ports_open=port)
            else:
                targets[host] = {'scheme': scheme, 'host': host, 'port': task.result()[5], 'path': path, 'ports_open': [task.result()[1]]}

    # 处理 fofa 的结果
    for _target in fofa_result:
        url = _target
        # scheme='http', netloc='www.baidu.com:80', path='', params='', query='', fragment=''
        scheme, netloc, path, params, query, fragment = urlparse(url, 'http')

        # 指定端口时需要，检查指定的端口是否开放
        host = netloc.split(':')[0]
        port = int(netloc.split(':')[1])
        if host in targets.keys() and (port == 80 or port == 443):
            pass
        else:
            fofa_target = {'scheme': scheme, 'host': netloc, 'port': port, 'path': path, 'ports_open': [port]}
            targets[netloc] = fofa_target

    return targets


# 使用异步协程， 检测目标80、443、给定端口是否开放
def process_targets(queue_targets, q_targets, args, fofa_result):

    sem = asyncio.Semaphore(1000)  # 限制并发量
    # 主线程通过 get_event_loop 获取当前事件循环， 对于其他线程需要首先loop=new_event_loop(),然后set_event_loop(loop)。
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 对目标和要扫描的端口做处理，格式化

    # queue_targets  ['http://127.0.0.1:8080', 'www.baidu.cn']
    # ip_port_list [('127.0.0.1', 8080, 0, 'http', ''), ('www.baidu.cn', 80, 0, 'unknown', ''), ('www.baidu.cn', 443, 0, 'unknown', '')]
    ip_port_list = get_ip_port_list(queue_targets, args)

    tasks = list()

    progress = Progress(
        "[progress.description]{task.description}",
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        TimeRemainingColumn(),
        "[bold red]{task.completed}/{task.total}",
        transient=True,  # 100%后隐藏进度条
    )

    # 进度条
    with progress:
        task = progress.add_task(f"[cyan]Start of port detection ...", total=len(queue_targets), start=False)
        try:
            for ip_port in ip_port_list:
                # 端口扫描任务
                tasks.append(loop.create_task(port_scan_check(ip_port, sem, progress, task)))
        except Exception as e:
            logger.log("ERROR", f'{e}')

        import platform
        if platform.system() != "Windows":
            import uvloop
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        loop.run_until_complete(asyncio.wait(tasks))
        # 关闭事件循环
        loop.close()

    # 对目标进行封装，格式化
    targets = get_target(tasks, fofa_result)

    for host in targets:
        target = targets[host]
        ports_open = target['ports_open']
        if 80 in ports_open and 443 in ports_open:
            target.update(port=443)
            target.update(scheme='https')

        elif 80 in ports_open:
            target.update(port=80)
            target.update(scheme='http')
        elif 443 in ports_open:
            target.update(port=443)
            target.update(scheme='https')

        if target['port'] in ports_open or 80 in ports_open or 443 in ports_open:
            target['has_http'] = True
        else:
            target['has_http'] = False
        # 添加目标，最终的扫描目标
        # {'scheme': 'https', 'host': '127.0.0.1', 'port': 443, 'path': '', 'ports_open': [443, 80], 'has_http': True}
        q_targets.put(target)

        logger.log("DEBUG", f'扫描目标详细信息: {target}')


# 解析域名获取ip，检查域名有效性 ，并保存有效url和ip
async def domain_lookup_check(loop, url, queue_targets, processed_targets):
    # 将 ['https://jd.com']  转换为 [('jd.com', 'https')]
    url = url.replace('\n', '').replace('\r', '').strip()
    if url.find('://') < 0:
        netloc = url[:url.find('/')] if url.find('/') > 0 else url
        scheme = 'http'
    else:
        scheme, netloc, path, params, query, fragment = urlparse(url, 'http')
    # host port
    if netloc.find(':') >= 0:
        _ = netloc.split(':')
        host = _[0]
    else:
        host = netloc

    # ('jd.com', 'https')
    url_tuple = (host, scheme)
    try:
        info = await loop.getaddrinfo(*url_tuple, proto=socket.IPPROTO_TCP,)
        queue_targets.append(url)
        for host in info:
            ip = host[4][0]
            # 只存IP， 为指定掩码做准备
            processed_targets.append(ip)
    except Exception as e:
        logger.log("ERROR", f'Invalid domain: {url}')
        pass


# 预处理 URL / IP / 域名，端口发现
def prepare_targets(target_list, q_targets, args):
    # 有效目标，包括url和ip
    queue_targets = []

    # 有效ip， 当指定其它掩码时，根据该ip添加目标
    processed_targets = []

    # 解析域名获取ip, 使用异步协程处理， 7000 有效 url ，解析ip大约 20s
    loop = asyncio.get_event_loop()
    tasks = [loop.create_task(domain_lookup_check(loop, url, queue_targets, processed_targets)) for url in target_list]
    # 使用uvloop加速asyncio, 目前不支持Windows
    import platform
    if platform.system() != "Windows":
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    result = asyncio.gather(*tasks)

    loop.run_until_complete(result)

    loop.close()

    logger.log("DEBUG", f'有效域名url: {queue_targets} 和 有效ip: {processed_targets}')

    # 对目标ip进行进一步处理， 检查是否存在cdn
    if args.checkcdn:
        processed_targets = check_cdn(target_list)

    logger.log("DEBUG", f'没有CDN的目标: {processed_targets}')

    # 当指定子网掩码时的处理逻辑, 将对应网段ip加入处理目标中
    if args.network != 32:
        for ip in processed_targets:
            if ip.find('/') > 0:    # 网络本身已经处理过 118.193.98/24
                continue
            _network = u'%s/%s' % ('.'.join(ip.split('.')[:3]), args.network)
            if _network in processed_targets:
                continue
            processed_targets.append(_network)
            if args.network >= 20:
                sub_nets = [ipaddress.IPv4Network(u'%s/%s' % (ip, args.network), strict=False).hosts()]
            else:
                sub_nets = ipaddress.IPv4Network(u'%s/%s' % (ip, args.network), strict=False).subnets(new_prefix=22)

            for sub_net in sub_nets:
                if sub_net in processed_targets:
                    continue
                if type(sub_net) == ipaddress.IPv4Network:    # add network only
                    processed_targets.append(str(sub_net))
                for _ip in sub_net:
                    _ip = str(_ip)
                    if _ip not in processed_targets:
                        queue_targets.append(_ip)

    # 目标列表 将域名和IP 合并
    queue_targets.extend(processed_targets)
    # 去重
    queue_targets = set(queue_targets)

    # fofa 扫到的并且存活的web资产
    fofa_result = []

    # 当配置 fofa api 时, 对 queue_targets 目标进行fofa搜索，扩大资产范围
    if fofaApi['email'] and fofaApi['key']:
        fofa_result = fmain(queue_targets)
    # fofa_result['http://127.0.0.1:3790', 'http://127.0.0.1:80', 'https://127.0.0.1:443']

    # 使用异步协程， 检测目标80、443、给定端口是否开放
    # 检测目标的80、443、给定端口是否开放，并格式化，加入扫描队列 q_targets
    process_targets(queue_targets, q_targets, args, fofa_result)
