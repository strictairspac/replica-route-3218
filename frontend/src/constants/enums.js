export const ROLES={admin:'管理员',operator:'操作员',viewer:'观察员'};
export const RUN_STATES={queued:'排队中',running:'执行中',completed:'已完成',failed:'失败'};
export const CRASH_EVENTS={write:'写入',commit:'提交',abort:'回滚',flush:'刷盘',read:'读取',crash:'断电',restart:'重启恢复'};
export const CRASH_DECISIONS={applied:'已应用',waiting:'等待事务结束',routed:'已路由',rejected:'拒绝',blocked:'提交被阻塞',cancelled:'已取消',flushed:'已刷盘',noop:'无需刷盘',crashed:'已断电',partial:'恢复中断',recovered:'恢复完成'};
