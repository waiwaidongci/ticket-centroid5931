# 001 初始表结构

应用首次启动以 SQLAlchemy 模型创建表与唯一约束。现有数据表只补建缺失表，升级到修改过字段的版本需要显式迁移。

- `users` 账号角色和加盐密码摘要。
- `datasets` 不可变记录批次，以名称和 SHA-256 去重。
- `runs` 记录参数、数据批次、运行租约、分析结果、导出文件及复核决定。
- `schedules` 保存定时计划和下一到期时间。
- `deliveries` 保存站内分发回执，每次运行最多一个回执。
- `audit_events` 记录登录、数据入库、运行、复核和报表分发动作。

# 002 排班推算

应用启动时由 `src/config/schema.py` 做幂等的列补齐，兼容已部署的数据库：

- `runs.kind`：`acceptance` 为分流验收，`staffing` 为排班推算；旧行回填为验收。
- `runs.basis_run_id`：排班推算所沿用的验收运行。
- `runs.request_key`：依据、件数与容差的 SHA-256 指纹，建唯一索引以保证重复提交只发布一次。
- `runs.resumes`：中断续算次数，与 `attempt` 分开计数，续算不消耗重试上限。
- `staffing_steps`：按 `(run_id, unit)` 唯一保存的边界检查点，中断后据此续算。
