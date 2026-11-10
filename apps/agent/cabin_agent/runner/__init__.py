"""cabin-runner：TaskPlan（cabin/taskplan/v1）的确定性执行器。

设计见 docs/EXECUTION_ARCHITECTURE.md。三条依赖规则：
1. 动作不判断对错，只操作并写证据；
2. 评估器不碰设备，只读证据（判定可离线重跑）；
3. 引擎不认识具体动作和断言类型，只通过注册表分发。
回放不注入模型客户端；vision / voice 断言经 JudgeClient 判定，属于云端判定。
"""
