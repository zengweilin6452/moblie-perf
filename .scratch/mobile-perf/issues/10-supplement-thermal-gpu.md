# 10: supplement 温度/GPU 补采 + 三源同图

**What to build:** supplement.py：PC 侧 dumpsys 轮询补采温度/GPU（perfcat 思路，§4 #8），对时后合并入库（source=pc_supplement，指标 gpu_util_pct / temp_battery_c）；报告三源（SoloPi / 补采 / 事件）同一时间轴同图呈现（§6.3）。

**Blocked by:** 04, 05

**Status:** ready-for-agent

- [ ] 温度曲线与帧率曲线同图且时间对齐（时钟偏移校准生效）
- [ ] 补采指标经指标字典入库（canonical key 合规，Q17）
- [ ] 无 root 真机可用；补采轮询不影响 SoloPi 采集（独立短事务）

## Comments
