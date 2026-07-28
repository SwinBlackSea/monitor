import { spawn } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const appUrl = process.argv[2] || "http://127.0.0.1:8765";
const profile = await mkdtemp(join(tmpdir(), "monitor-chromium-"));
const chromium = process.env.CHROMIUM || "/snap/bin/chromium";
const debugPort = 20000 + (process.pid % 20000);
const browser = spawn(chromium, [
  "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
  `--remote-debugging-port=${debugPort}`, `--user-data-dir=${profile}`, "about:blank",
], { stdio: "ignore" });

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const deadline = Date.now() + 15000;
let browserReady = false;
while (Date.now() < deadline) {
  try {
    const response = await fetch(`http://127.0.0.1:${debugPort}/json/version`);
    if (!response.ok) throw new Error("not ready");
    browserReady = true;
    break;
  } catch {
    await sleep(100);
  }
}
if (!browserReady) throw new Error("Chromium 未能启动远程调试端口");

const targetResponse = await fetch(`http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(appUrl)}`, { method: "PUT" });
const target = await targetResponse.json();
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});

let sequence = 0;
const pending = new Map();
socket.addEventListener("message", event => {
  const message = JSON.parse(event.data);
  if (!message.id || !pending.has(message.id)) return;
  const { resolve, reject } = pending.get(message.id);
  pending.delete(message.id);
  message.error ? reject(new Error(message.error.message)) : resolve(message.result);
});
function send(method, params = {}) {
  const id = ++sequence;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}
async function evaluate(expression) {
  const result = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}
async function waitFor(expression, message, timeout = 12000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await evaluate(expression)) return;
    await sleep(100);
  }
  throw new Error(`等待超时：${message}`);
}
function assert(condition, message) {
  if (!condition) throw new Error(`断言失败：${message}`);
}

try {
  await send("Page.enable");
  await send("Runtime.enable");
  await waitFor("document.readyState === 'complete' && document.querySelectorAll('tbody tr').length > 0", "页面数据加载");
  await waitFor("Number(document.documentElement.dataset.hostStatusRefresh) > 0", "主机连通状态缓存加载");

  const initial = await evaluate(`(() => ({
    title: document.querySelector("h1")?.textContent,
    tabs: document.querySelectorAll(".machine").length,
    addButton: Boolean(document.querySelector("[data-add]")),
    tabDots: document.querySelectorAll(".machine .dot").length,
    tabPulses: document.querySelectorAll(".machine .connectivity-dot.pulse").length,
    collectionPulses: document.querySelectorAll("#summary .collection-dot.pulse").length,
    addressDots: document.querySelectorAll(".statusline .dot").length,
    columns: document.querySelectorAll("thead [data-sort]").length,
    parents: [...document.querySelectorAll("tbody .parent")].map(x => x.textContent.trim()),
    cpu: [...document.querySelectorAll("tbody .metric")].map(x => parseFloat(x.textContent)),
    bold: [...document.querySelectorAll("tbody td")].some(x => Number(getComputedStyle(x).fontWeight) >= 600),
    duplicateIds: [...document.querySelectorAll("[id]")].map(x => x.id).filter((id, i, all) => all.indexOf(id) !== i)
  }))()`);
  assert(initial.title === "进程监视器", "标题与原型一致");
  assert(initial.tabs === 4 && initial.tabDots === 4 && initial.tabPulses === 4 && initial.collectionPulses === 1 && initial.addressDots === 0, "每个机器 Tab 显示 SSH 连通点，采集时间前独立显示资源采集点");
  assert(initial.addButton, "机器 Tab 末尾显示添加按钮");
  assert(initial.columns === 6, "进程表所有数据列均可排序");
  assert(initial.parents.every(Boolean), "父进程名称和 PID 已展示");
  assert(initial.cpu.every((v, i, a) => i === 0 || a[i - 1] >= v), "CPU 默认从高到低");
  assert(!initial.bold, "表格内容使用常规字重");
  assert(initial.duplicateIds.length === 0, "页面不存在重复 ID");
  const statusLine = await evaluate(`({
    oldUpdate: Boolean(document.querySelector("#updated")),
    sample: document.querySelector("#sampleTime")?.textContent,
    currentExists: Boolean(document.querySelector("#currentTime")),
    clockLabels: [...document.querySelectorAll("#summary .clock-sum .sum-label")].map(x => x.textContent),
    clocksInSummary: Boolean(document.querySelector("#summary #sampleTime")),
    clocksInStatus: Boolean(document.querySelector(".statusline #currentTime, .statusline #sampleTime")),
    summaryPositions: (window.__cpuSummaryPositions=[...document.querySelectorAll("#summary .sum")].slice(0,3).map(x => x.getBoundingClientRect().left)),
    summaryHeight: document.querySelector("#summary").getBoundingClientRect().height,
    summarySeparators: [...document.querySelectorAll("#summary .sum")].some(x => parseFloat(getComputedStyle(x).borderRightWidth)>0) || parseFloat(getComputedStyle(document.querySelector("#summary")).borderBottomWidth)>0,
    compactColumns: [...document.querySelectorAll("#summary .sum")].slice(0,3).map(x => x.getBoundingClientRect().left).every((x,i,a)=>i===0||x-a[i-1]<=128),
    collectionBeforeTime: Boolean(document.querySelector("#collectionStatusDot")?.compareDocumentPosition(document.querySelector("#sampleTime")) & Node.DOCUMENT_POSITION_FOLLOWING),
    endpoint: document.querySelector("#hostEndpoint")?.textContent,
    loopbackDisplay: displayAddress({address:"127.0.0.1",local:true}),
    tunnelDisplay: displayAddress({address:"127.0.0.1",port:2222,local:false,tunnel_peer:"198.51.100.8"}),
    normalDisplay: displayAddress({address:"10.0.0.8",port:22,local:false}),
    browserHost: location.hostname,
    collectionDot: Boolean(document.querySelector("#collectionStatusDot.normal.pulse")),
    collectionLabel: document.querySelector("#collectionStatusDot")?.getAttribute("aria-label"),
    tabDots: document.querySelectorAll(".machine .connectivity-dot").length,
    addressDots: document.querySelectorAll(".statusline .dot").length
  })`);
  assert(!statusLine.oldUpdate, "右上角更新时间已移除");
  assert(statusLine.sample && !statusLine.currentExists && statusLine.clockLabels.join(",")==="采集时间" && statusLine.clocksInSummary && !statusLine.clocksInStatus && statusLine.collectionBeforeTime, "CPU 概要栏的逻辑核心之后只保留带独立状态点的采集时间");
  assert(statusLine.summaryHeight <= 30 && !statusLine.summarySeparators && statusLine.compactColumns, "资源摘要无分隔线、纵向紧凑且字段列固定");
  assert(statusLine.endpoint.startsWith("SSH 地址：") && statusLine.loopbackDisplay === statusLine.browserHost && statusLine.tunnelDisplay === "198.51.100.8" && statusLine.normalDisplay === "10.0.0.8" && statusLine.collectionDot && statusLine.collectionLabel === "资源采集成功" && statusLine.tabDots === 4 && statusLine.addressDots === 0, "采集状态点位于采集时间前，地址行只负责本机、SSH 与反向隧道地址");
  assert(await evaluate(`(() => {const host=activeHost(),connectivity=host.connectivity_status,collection=host.collection_status,status=host.status;host.connectivity_status="offline";host.collection_status="normal";host.status="normal";renderTabs();renderSummary();const separate=Boolean(document.querySelector(".machine.active .connectivity-dot.offline")&&document.querySelector("#collectionStatusDot.normal"));host.connectivity_status=connectivity;host.collection_status=collection;host.status=status;renderTabs();renderSummary();return separate})()`), "机器 SSH 连通状态与当前资源采集状态使用两套独立逻辑");
  const hostStatusRequest = await evaluate(`(async()=>{window.__hostStatusBaseFetch=window.fetch;window.__hostStatusFetches=[];window.fetch=(...args)=>{window.__hostStatusFetches.push(String(args[0]));return window.__hostStatusBaseFetch(...args)};await refreshHostStatuses();const result={health:window.__hostStatusFetches.filter(x=>x==="/api/host-statuses").length,snapshots:window.__hostStatusFetches.filter(x=>x.includes("/snapshot")).length};window.fetch=window.__hostStatusBaseFetch;return result})()`);
  assert(hostStatusRequest.health === 1 && hostStatusRequest.snapshots === 0, "浏览器状态轮询只读取轻量缓存接口，不触发 SSH 或资源快照");
  assert(await evaluate(`document.querySelectorAll("#sampleTime .time-colon").length === 2 && getComputedStyle(document.querySelector("#sampleTime .time-colon")).animationName === "clockPulse"`), "采集时间冒号按秒轻微跳动");
  assert(await evaluate(`!document.querySelector("#currentTime, #sampleTime .late, #sampleTime [title]") && getComputedStyle(document.querySelector("#sampleTime")).color!=="rgb(217, 45, 32)"`), "采集时间不使用延迟红字或悬浮提示");

  await evaluate(`document.querySelector('[data-sort="name"]').click()`);
  const names = await evaluate(`[...document.querySelectorAll("tbody .proc-cell")].map(x => x.firstChild.textContent.trim())`);
  assert(names.every((v, i, a) => i === 0 || a[i - 1].localeCompare(v, "zh-CN", {numeric:true}) <= 0), "名称列升序排序");

  const immediateMemory = await evaluate(`document.querySelector('[data-view="memory"]').click(); ({active:document.querySelector('[data-view="memory"]').classList.contains("active"), table:document.querySelector("th.metric")?.textContent.includes("内存"), rows:document.querySelectorAll("tbody tr").length, summaryStable:JSON.stringify([...document.querySelectorAll("#summary .sum")].slice(0,3).map(x => x.getBoundingClientRect().left))===JSON.stringify(window.__cpuSummaryPositions)})`);
  assert(immediateMemory.active && immediateMemory.table && immediateMemory.rows > 0 && immediateMemory.summaryStable, "内存切换立即显示缓存数据且概要字段位置固定");
  await waitFor(`document.querySelector('[data-view="memory"]').classList.contains("active") && document.querySelector("th.metric")?.textContent.includes("内存")`, "切换内存页");
  const memory = await evaluate(`[...document.querySelectorAll("tbody .metric")].map(x => parseFloat(x.textContent))`);
  assert(memory.every((v, i, a) => i === 0 || a[i - 1] >= v), "内存默认从高到低");
  await evaluate(`document.querySelector('[data-host="demo-2"]').click()`);
  await waitFor(`document.querySelector('[data-host="demo-2"]').classList.contains("active")`, "切换第二台机器");
  assert(await evaluate(`document.querySelector('[data-view="cpu"]').classList.contains("active")`), "首次打开机器默认 CPU");
  await evaluate(`document.querySelector('[data-host="demo-1"]').click()`);
  await waitFor(`document.querySelector('[data-host="demo-1"]').classList.contains("active")`, "切回第一台机器");
  assert(await evaluate(`document.querySelector('[data-view="memory"]').classList.contains("active") && document.querySelectorAll("tbody tr").length>0`), "切回机器立即显示该机器上次缓存并保留资源页签");
  const refreshStart = await evaluate(`Number(document.documentElement.dataset.refreshCount);window.__summaryLabels=[...document.querySelectorAll("#summary .sum-label")];window.__snapshotFetches=[];window.__snapshotBaseFetch=window.fetch;window.fetch=(...args)=>{window.__snapshotFetches.push(String(args[0]));return window.__snapshotBaseFetch(...args)};refresh({animate:false,refreshHosts:false});Number(document.documentElement.dataset.refreshCount)`);
  await waitFor(`Number(document.documentElement.dataset.refreshCount) > ${refreshStart}`, "当前机器缓存刷新");
  const batchRefresh = await evaluate(`({selected:window.__snapshotFetches.filter(url=>url==="/api/hosts/demo-1/snapshot").length,batch:window.__snapshotFetches.filter(url=>url==="/api/snapshots").length,other:window.__snapshotFetches.filter(url=>url.includes("/snapshot")&&url!=="/api/hosts/demo-1/snapshot").length,hosts:window.__snapshotFetches.filter(url=>url==="/api/hosts").length})`);
  await evaluate(`window.fetch=window.__snapshotBaseFetch`);
  assert(batchRefresh.selected === 1 && batchRefresh.batch === 0 && batchRefresh.other === 0 && batchRefresh.hosts === 0, "每轮只采集当前机器且不查询其他机器");
  assert(await evaluate(`[...document.querySelectorAll("#summary .sum-label")].every((node,index)=>node===window.__summaryLabels[index])`), "同一资源页刷新只更新摘要数值，字段节点与位置保持不动");

  await evaluate(`document.querySelector('[data-view="disk"]').click()`);
  await waitFor(`document.querySelector('[data-view="disk"]').classList.contains("active") && document.querySelectorAll("tbody tr").length > 0`, "切换硬盘页");
  const disk = await evaluate(`({
    processKill: document.querySelectorAll(".kill:not(.clear-dir)").length,
    mounts: document.querySelectorAll('tbody tr[data-depth="0"]').length,
    values: [...document.querySelectorAll("tbody .tree-percent")].map(x => parseFloat(x.textContent)),
    columns: document.querySelectorAll("thead [data-sort]").length,
    clocks: document.querySelectorAll("#currentTime, #sampleTime").length,
    summaryStable: JSON.stringify([...document.querySelectorAll("#summary .sum")].slice(0,3).map(x => x.getBoundingClientRect().left))===JSON.stringify(window.__cpuSummaryPositions)
  })`);
  assert(disk.processKill === 0 && disk.mounts === 2 && disk.clocks === 0 && disk.summaryStable, "硬盘页只显示挂载点，概要字段位置固定且不显示采集时钟");
  assert(disk.columns === 3, "硬盘树数据列均可排序");
  assert(disk.values.every((v, i, a) => i === 0 || a[i - 1] >= v), "硬盘使用率默认从高到低");
  const diskRefreshStart = await evaluate(`window.__diskTable=document.querySelector(".disk-tree");const before=Number(document.documentElement.dataset.refreshCount);refresh({animate:true,refreshHosts:false});before`);
  await waitFor(`Number(document.documentElement.dataset.refreshCount) > ${diskRefreshStart}`, "硬盘页后台采集");
  assert(await evaluate(`document.querySelector(".disk-tree")===window.__diskTable && getComputedStyle(document.querySelector(".tree-row")).animationName==="none"`), "硬盘后台采集只更新缓存，不重建硬盘表格 DOM");
  assert(await evaluate(`(() => {const entry=diskTreeEntry("/","/"),small={name:"small",path:"/small",size_bytes:1,percent:.1,depth:1,can_delete:false},large={name:"large",path:"/large",size_bytes:100,percent:10,depth:1,can_delete:false};entry.expanded=true;entry.loading=true;entry.children=[small,large];const during=flattenDiskTree(),hidden=during.filter(row=>row.depth===1&&row.kind==="node").length===0&&during.some(row=>row.kind==="loading");entry.loading=false;const after=flattenDiskTree().filter(row=>row.depth===1&&row.kind==="node").map(row=>row.name).join(",");entry.expanded=false;entry.children=null;return hidden&&after==="large,small"})()`), "后端线程全部完成后一次性显示完整排序结果");
  assert(await evaluate(`(() => {const entry=diskTreeEntry("/","/");entry.expanded=true;entry.loading=false;entry.children=[];entry.warning="无法读取 /home/lighthouse：Permission denied，当前占用结果可能不完整";const warning=flattenDiskTree().find(row=>row.kind==="warning"),html=warning?diskTreeRow(warning):"";entry.expanded=false;entry.children=null;entry.warning=null;return warning?.message.includes("/home/lighthouse")&&html.includes("tree-warning")&&html.includes("Permission denied")})()`), "远程目录部分失败时保留结果并在树内显示具体路径和权限原因");
  await evaluate(`window.__diskRefreshNote=document.querySelector("#refreshNote").textContent`);

  await evaluate(`document.querySelector('[data-tree-toggle][data-path="/"]').click()`);
  await waitFor(`document.querySelector('[data-tree-toggle][data-path="/home"]')`, "展开挂载点一级目录");
  await evaluate(`document.querySelector('[data-tree-toggle][data-path="/home"]').click()`);
  await waitFor(`document.querySelector('[data-tree-toggle][data-path="/home/devhost"]')`, "展开二级目录");
  assert(await evaluate(`document.querySelector('[data-tree-toggle][data-path="/home/devhost"]').closest("tr").querySelector(".tree-title").textContent === "~/"`), "用户主目录缩写为 ~/");
  await evaluate(`document.querySelector('[data-tree-toggle][data-path="/home/devhost"]').click()`);
  await waitFor(`document.querySelector('[data-tree-toggle][data-path="/home/devhost/projects"]')`, "展开三级目录");
  const levelThree = await evaluate(`(() => {const button=document.querySelector('.clear-dir[data-path="/home/devhost/projects"]');return {exists:Boolean(button),enabled:Boolean(button&&!button.disabled),depth:button?.closest("tr").dataset.depth,nativeTitle:button?.hasAttribute("title")}})()`);
  assert(levelThree.exists && levelThree.enabled && levelThree.depth === "3" && !levelThree.nativeTitle, "三级目录显示清空按钮且不使用重复触发的原生提示");
  assert(await evaluate(`(() => {const entry={loading:false,children:null,expanded:false},base={kind:"node",isMount:false,mount:"/",name:"target",path:"/one/two/three",size_bytes:1,percent:.1,total_bytes:100,entry,delete_min_depth:4};return !diskTreeRow({...base,depth:3,can_delete:false}).includes("clear-dir")&&diskTreeRow({...base,path:"/one/two/three/four",depth:4,can_delete:true}).includes("clear-dir")})()`), "--allow-delete N 同步控制前端从第 N 级显示清空按钮");
  assert(await evaluate(`(() => {const button=document.querySelector('.clear-dir[data-path="/home/devhost/projects"]');button.setAttribute("aria-disabled","true");button.dataset.hint="使用 --allow-delete 3 启动后可清空";button.dispatchEvent(new MouseEvent("mouseover",{bubbles:true}));const shown=document.querySelector("#hoverHint").classList.contains("show")&&document.querySelector("#hoverHint").textContent.includes("--allow-delete 3");button.removeAttribute("aria-disabled");delete button.dataset.hint;hideHoverHint();return shown})()`), "禁用的目录清空按钮显示稳定的自定义悬浮说明");
  await evaluate(`document.querySelector('[data-tree-toggle][data-path="/home/devhost/projects"]').click()`);
  await waitFor(`document.querySelector('[data-tree-toggle][data-path="/home/devhost/projects/monitor"]')`, "三级目录可继续展开");
  await evaluate(`document.querySelector('[data-tree-toggle][data-path="/home"]').click();document.querySelector('[data-tree-toggle][data-path="/home"]').click()`);
  assert(await evaluate(`Boolean(document.querySelector('[data-tree-toggle][data-path="/home/devhost"]')) && !document.querySelector('[data-tree-toggle][data-path="/home/devhost/projects"]')`), "收起父目录会递归收起全部子树");
  await evaluate(`document.querySelector('[data-tree-toggle][data-path="/home/devhost"]').click();document.querySelector('[data-tree-toggle][data-path="/home/devhost/projects"]').click()`);
  await waitFor(`document.querySelector('[data-tree-toggle][data-path="/home/devhost/projects/monitor"]')`, "递归收起后可逐层重新展开");
  if (process.env.MONITOR_TREE_SCREENSHOT) {
    await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
    await sleep(150);
    const treeShot = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
    await writeFile(process.env.MONITOR_TREE_SCREENSHOT, Buffer.from(treeShot.data, "base64"));
  }
  await evaluate(`document.querySelector('.clear-dir[data-path="/home/devhost/projects"]').click()`);
  const clearConfirmation = await evaluate(`document.querySelector("#clearText").textContent`);
  assert(clearConfirmation.includes("目录本身会保留") && clearConfirmation.includes("无法撤销"), "清空确认明确保留目录且不可撤销");
  if (process.env.MONITOR_SCREENSHOT) {
    const shot = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
    await writeFile(process.env.MONITOR_SCREENSHOT, Buffer.from(shot.data, "base64"));
  }
  await evaluate(`document.querySelector("#confirmClear").click()`);
  await waitFor(`document.querySelector('[data-tree-toggle][data-path="/home/devhost/projects"]') && document.querySelector('.clear-dir[data-path="/home/devhost/projects"]').closest("tr").querySelector(".tree-size").textContent.includes("0 B")`, "清空后目录保留且占用归零");
  await evaluate(`document.querySelector('[data-tree-toggle][data-path="/home/devhost/projects"]').click()`);
  await waitFor(`document.querySelector('[data-tree-toggle][data-path="/home/devhost/projects"]').disabled`, "已清空目录展开后显示为空");

  await evaluate(`document.querySelector('[data-view="cpu"]').click()`);
  await waitFor(`document.querySelector('[data-view="cpu"]').classList.contains("active")`, "返回 CPU 页");
  assert(await evaluate(`window.__diskRefreshNote.includes("点击目录时查询") && window.__diskRefreshNote.includes("10 分钟") && document.querySelector("#refreshNote").textContent.includes("每 5 秒")`), "页脚说明随硬盘与进程视图正确切换");
  const beforeRefresh = await evaluate(`Number(document.documentElement.dataset.refreshCount)`);
  await waitFor(`Number(document.documentElement.dataset.refreshCount) > ${beforeRefresh}`, "5 秒自动刷新", 8000);
  const refreshed = await evaluate(`({
    values: [...document.querySelectorAll("tbody .metric")].map(x => parseFloat(x.textContent)),
    animated: Number(document.documentElement.dataset.lastAnimated || 0)
  })`);
  assert(refreshed.values.every((v, i, a) => i === 0 || a[i - 1] >= v), "刷新后仍按占用比例降序");
  assert(refreshed.animated > 0, "刷新后的行具有数值或位移动画");

  await evaluate(`document.querySelector(".machine.active .rename").click()`);
  await sleep(100);
  const editorDesign = await evaluate(`(() => {const input=document.querySelector("#hostName"),style=getComputedStyle(input);return {outline:style.outlineStyle,border:style.borderTopWidth,test:document.querySelector("#testHost").textContent,save:document.querySelector("#saveHost").textContent}})()`);
  assert(editorDesign.outline === "none" && editorDesign.border === "1px", "编辑机器输入框使用单层选中边框");
  await evaluate(`document.querySelector("#testHost").click()`);
  await waitFor(`document.querySelector("#connectionCheck").classList.contains("success")`, "独立测试机器连接");
  assert(await evaluate(`document.querySelector("#hostDialog").open && document.querySelector("#testHost").textContent==="测试连接" && document.querySelector("#saveHost").textContent==="保存"`), "测试连接与保存是两个独立操作");
  await evaluate(`document.querySelector("#hostName").value="我的开发机"; document.querySelector("#saveHost").click()`);
  await waitFor(`[...document.querySelectorAll(".machine")].some(x => x.textContent.includes("我的开发机"))`, "机器编辑");

  const validation = await evaluate(`document.querySelector("[data-add]").click(); document.querySelector("#saveHost").click(); document.querySelector("#hostFormStatus").textContent`);
  assert(validation.includes("机器名称"), "新增失败原因在弹窗内持续显示");
  const inputTypes = await evaluate(`({address:document.querySelector("#hostAddress").tagName,user:document.querySelector("#hostUser").tagName})`);
  assert(inputTypes.address === "INPUT" && inputTypes.user === "INPUT", "机器连接字段读取真实输入框");
  await evaluate(`window.__baseFetch=window.fetch;window.__delayedHostList=false;window.fetch=(...args)=>{const method=args[1]?.method||"GET";if(String(args[0])==="/api/hosts"&&method==="GET"&&!window.__delayedHostList){window.__delayedHostList=true;return window.__baseFetch(...args).then(response=>new Promise(resolve=>setTimeout(()=>resolve(response),2000)))}return window.__baseFetch(...args)};refresh({animate:false,refreshHosts:true})`);
  await sleep(100);
  const addStarted = Date.now();
  await evaluate(`document.querySelector("#hostName").value="11"; document.querySelector("#hostAddress").value="127.0.0.1"; document.querySelector("#hostUser").value="devhost"; document.querySelector("#hostPort").value="2222"; document.querySelector("#saveHost").click()`);
  await waitFor(`document.querySelector(".machine.active")?.textContent.includes("11") && document.querySelectorAll("tbody tr").length > 0`, "新增后立即切换并显示快照", 900);
  assert(Date.now() - addStarted < 900, "新增机器不等待进行中的旧刷新");
  await evaluate(`window.fetch=window.__baseFetch`);
  assert(await evaluate(`document.querySelectorAll(".machine").length === 5`), "新增后 Tab 数量增加");
  await evaluate(`window.__deleteBaseFetch=window.fetch;window.__staleDeleteList=false;window.fetch=(...args)=>{const url=String(args[0]),method=args[1]?.method||"GET";if(url==="/api/hosts"&&method==="GET"&&!window.__staleDeleteList){window.__staleDeleteList=true;return window.__deleteBaseFetch(...args).then(response=>new Promise(resolve=>setTimeout(()=>resolve(response),2000)))}if(url.includes("/api/hosts/11")&&method==="DELETE")return new Promise(resolve=>setTimeout(()=>resolve(window.__deleteBaseFetch(...args)),1000));return window.__deleteBaseFetch(...args)};refresh({animate:false,refreshHosts:true})`);
  await sleep(100);
  const immediateDelete = await evaluate(`document.querySelector(".machine.active .rename").click();document.querySelector("#removeHost").click();document.querySelector("#confirmDelete").click();({tabs:document.querySelectorAll(".machine").length,active:document.querySelector(".machine.active")?.textContent,rows:document.querySelectorAll("tbody tr").length})`);
  assert(immediateDelete.tabs === 4 && !immediateDelete.active.includes("11") && immediateDelete.rows > 0, "删除 active 机器后立即切到缓存机器");
  await sleep(2200);
  assert(await evaluate(`document.querySelectorAll(".machine").length === 4`), "迟到的旧刷新不会复活已删除机器");
  await evaluate(`window.fetch=window.__deleteBaseFetch`);

  await evaluate(`document.querySelector("[data-add]").click();document.querySelector("#hostName").value="回滚节点";document.querySelector("#hostAddress").value="10.0.9.9";document.querySelector("#hostUser").value="monitor";document.querySelector("#hostPort").value="22";document.querySelector("#saveHost").click()`);
  await waitFor(`document.querySelector(".machine.active")?.textContent.includes("回滚节点")`, "创建删除失败测试节点");
  const rollbackImmediate = await evaluate(`window.__rollbackBaseFetch=window.fetch;window.fetch=(...args)=>args[1]?.method==="DELETE"?Promise.resolve(new Response(JSON.stringify({error:"模拟删除失败"}),{status:500,headers:{"Content-Type":"application/json"}})):window.__rollbackBaseFetch(...args);document.querySelector(".machine.active .rename").click();document.querySelector("#removeHost").click();document.querySelector("#confirmDelete").click();document.querySelectorAll(".machine").length`);
  assert(rollbackImmediate === 4, "删除请求返回前立即移除 Tab");
  await waitFor(`document.querySelectorAll(".machine").length === 5 && document.querySelector(".machine.active")?.textContent.includes("回滚节点")`, "删除失败完整恢复机器");
  await evaluate(`window.fetch=window.__rollbackBaseFetch;document.querySelector(".machine.active .rename").click();document.querySelector("#removeHost").click();document.querySelector("#confirmDelete").click()`);
  await waitFor(`document.querySelectorAll(".machine").length === 4`, "清理删除失败测试节点");

  await evaluate(`document.querySelector("[data-add]").click();document.querySelector("#hostName").value="非当前节点";document.querySelector("#hostAddress").value="10.0.9.10";document.querySelector("#hostUser").value="monitor";document.querySelector("#hostPort").value="22";document.querySelector("#saveHost").click()`);
  await waitFor(`document.querySelector(".machine.active")?.textContent.includes("非当前节点")`, "创建非 active 删除测试节点");
  await evaluate(`document.querySelector('[data-host="demo-1"]').click()`);
  await waitFor(`document.querySelector('[data-host="demo-1"]').classList.contains("active")`, "切回原机器");
  const inactiveDelete = await evaluate(`document.querySelector('[data-manage="host"]').click();document.querySelector("#removeHost").click();document.querySelector("#confirmDelete").click();({tabs:document.querySelectorAll(".machine").length,active:document.querySelector(".machine.active")?.dataset.host})`);
  assert(inactiveDelete.tabs === 4 && inactiveDelete.active === "demo-1", "删除非 active 机器不影响当前页面");

  const killTarget = await evaluate(`({count:document.querySelectorAll("tbody tr").length,pid:Number(document.querySelector(".kill:not(:disabled)").dataset.pid)})`);
  const immediateKill = await evaluate(`window.__monitorFetch=window.fetch;window.fetch=(...args)=>String(args[0]).includes("/terminate")?new Promise(resolve=>setTimeout(()=>resolve(window.__monitorFetch(...args)),800)):window.__monitorFetch(...args);document.querySelector(".kill:not(:disabled)").click();document.querySelector("#confirmKill").click();document.querySelectorAll("tbody tr").length`);
  assert(immediateKill < killTarget.count, "确认终止后立即从当前表格移除进程");
  await evaluate(`document.querySelector('[data-view="memory"]').click()`);
  assert(await evaluate(`!document.querySelector('tbody tr[data-row-id="${killTarget.pid}"]')`), "CPU 和内存缓存同步移除被终止进程");
  await evaluate(`document.querySelector('[data-view="cpu"]').click()`);
  await waitFor(`document.querySelectorAll("tbody tr").length < ${killTarget.count}`, "终止进程");

  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await sleep(200);
  const mobile = await evaluate(`({
    overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    parentInline: document.querySelector(".proc-sub")?.textContent.includes("父进程"),
    background: getComputedStyle(document.body).backgroundColor
  })`);
  assert(!mobile.overflow && mobile.parentInline, "移动端无横向溢出且保留父进程信息");
  assert(mobile.background === "rgb(255, 255, 255)", "页面保持白色背景");

  console.log("Browser E2E: 70/70 checks passed");
} finally {
  socket.close();
  browser.kill("SIGTERM");
  await rm(profile, { recursive: true, force: true });
}
