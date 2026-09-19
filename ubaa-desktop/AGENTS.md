# Project Instructions — UBAA Desktop

本项目由 `project-owner:ubaa-desktop` 作为唯一 Project Owner Agent。

开始工作前依次读取：

1. `.harness/project.json`
2. `.harness/PROJECT.md`
3. `.harness/PRODUCT.md`
4. `.harness/ARCHITECTURE.md`
5. `.harness/PLAN.md`
6. `.harness/STATUS.md`

所有实现必须绑定到产品验收标准或经记录的维护目标。重要技术决定写入 `.harness/decisions/`；验证输出、截图和日志写入 `.harness/evidence/`；外部 reviewer 只写入 `.harness/reviews/`。

使用仓库根目录上方 harness 的 `scripts/set_status.py` 更新机器状态，不要手工修改生成的 `STATUS.md`。

项目专属补充规则可以写在本文件末尾，但不能降低全局安全边界和质量门。
