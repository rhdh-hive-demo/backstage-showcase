import { createRouter } from '@backstage/plugin-app-backend';
import type { Router } from 'express';
import type { PluginEnvironment } from '../types';

export default async function createPlugin(
  env: PluginEnvironment,
): Promise<Router> {
  return await createRouter({
    logger: env.logger,
    config: env.config,
    database: env.database,
    appPackageName: 'app',
  });
}
