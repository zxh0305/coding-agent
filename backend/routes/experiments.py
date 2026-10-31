"""Authenticated, owner-checked experiment API; no paths accepted from browsers."""
import urllib.parse

from experiments.web import manager, WebError, safe


class ExperimentRoutes:
    def _handle_experiments(self, path, method):
        try:
            parts = path.strip('/').split('/')
            user = self.user['id']
            if len(parts) == 2:
                if method == 'GET':
                    return self._json({'experiments': manager.listing(user)})
                body = self._body()
                return self._json({'run_id': manager.create(body, user)}, 202)
            rid = parts[2]
            action = parts[3] if len(parts) == 4 else ''
            if len(parts) > 4:
                raise WebError('未知实验接口', 404)
            query = self._query()
            if method == 'POST' and action == 'stop':
                if self._body() != {}:
                    raise WebError('停止操作不接受额外字段')
                return self._json(manager.stop(rid, user))
            if method != 'GET':
                raise WebError('未知实验接口', 404)
            if action in ('', 'summary'):
                return self._json(manager.detail(rid, user, full=not action))
            if action in ('events', 'requests'):
                try:
                    cursor = int(query.get('cursor', ['0'])[0])
                    limit = int(query.get('limit', ['50'])[0])
                except ValueError:
                    raise WebError('游标或数量非法')
                return self._json(manager.logs(rid, user, action, cursor, limit,
                                              raw=query.get('raw', ['0'])[0] == '1'))
            if action in ('artifact', 'download'):
                artifact = query.get('id', [''])[0]
                target = manager.artifact(rid, user, artifact)
                if action == 'artifact':
                    return self._json(dict(id=artifact, name=target.name,
                                           content=safe(target.read_text(encoding='utf-8'))))
                data = target.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(target.name))
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            raise WebError('未知实验接口', 404)
        except WebError as e:
            return self._json({'error': str(e)}, e.status)
        except (UnicodeDecodeError, OSError):
            return self._json({'error': '实验记录不可读或已损坏'}, 422)
