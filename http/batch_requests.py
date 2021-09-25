import json
import asyncio
import aiohttp
import mlrun
from storey.flow import _ConcurrentJobExecution

class BatchHttpRequests(_ConcurrentJobExecution):
    """class for calling remote endpoints in parallel
    """

    def __init__(
        self,
        url: str = None,
        subpath: str = None,
        method: str = None,
        headers: dict = None,
        url_expression: str = None,
        body_expression: str = None,
        return_json: bool = True,
        input_path: str = None,
        result_path: str = None,
        **kwargs,
    ):
        """class for calling remote endpoints in parallel

        sync and async graph step implementation for request/resp to remote service (class shortcut = "$remote")
        url can be an http(s) url (e.g. "https://myservice/path") or an mlrun function uri ([project/]name).
        alternatively the url_expression can be specified to build the url from the event (e.g. "event['url']").

        example pipeline::

            flow = function.set_topology("flow", engine="async")
            flow.to("BatchRemoteStep",
                    url_expression="event['url']",
                    body_expression="event['data']",
                    input_path="req",
                    result_path="resp",
                    ).respond()

            server = function.to_mock_server()
            resp = server.test(body={"req": [{"url": url, "data": data}]})


        :param url:     http(s) url or function [project/]name to call
        :param subpath: path (which follows the url)
        :param method:  HTTP method (GET, POST, ..), default to POST
        :param headers: dictionary with http header values
        :param url_expression: an expression for getting the url from the event, e.g. "event['url']"
        :param body_expression: an expression for getting the request body from the event, e.g. "event['data']"
        :param return_json: indicate the returned value is json, and convert it to a py object
        :param input_path:  when specified selects the key/path in the event to use as body
                            this require that the event body will behave like a dict, example:
                            event: {"data": {"a": 5, "b": 7}}, input_path="data.b" means request body will be 7
        :param result_path: selects the key/path in the event to write the results to
                            this require that the event body will behave like a dict, example:
                            event: {"x": 5} , result_path="resp" means the returned response will be written
                            to event["y"] resulting in {"x": 5, "resp": <result>}
        """
        if url and url_expression:
            raise mlrun.errors.MLRunInvalidArgumentError(
                "cannot set both url and url_expression"
            )
        self.url = url
        self.url_expression = url_expression
        self.body_expression = body_expression
        self.headers = headers
        self.method = method
        self.return_json = return_json
        self.subpath = subpath or ""
        super().__init__(
            input_path=input_path, result_path=result_path, **kwargs
        )

        self._append_event_path = False
        self._endpoint = ""
        self._session = None
        self._url_function_handler = None
        self._body_function_handler = None

    def _init(self):
        super()._init()
        self._client_session = None

    async def _lazy_init(self):
        connector = aiohttp.TCPConnector()
        self._client_session = aiohttp.ClientSession(connector=connector)

    async def _cleanup(self):
        await self._client_session.close()

    def post_init(self, mode="sync"):
        self._endpoint = self.url
        if self.url and self.context:
            self._endpoint = self.context.get_remote_endpoint(self.url).strip("/")
        if self.body_expression:
            # init lambda function for calculating url from event
            self._body_function_handler = eval(
                "lambda event: " + self.body_expression, {}, {}
            )
        if self.url_expression:
            # init lambda function for calculating url from event
            self._url_function_handler = eval(
                "lambda event: " + self.url_expression, {}, {"endpoint": self._endpoint}
            )
        elif self.subpath:
            self._endpoint = self._endpoint + "/" + self.subpath.lstrip("/")

    async def _process_event(self, event):
        # async implementation (with storey)
        method = self.method or event.method or "POST"
        headers = self.headers or event.headers or {}
        input_list = self._get_event_or_body(event)
        is_get = method == "GET"
        is_json = False

        body_list = []
        url_list = []
        for body in input_list:
            if self._url_function_handler:
                url_list.append(self._url_function_handler(body))
            else:
                url_list.append(self._endpoint)

            if is_get:
                body = None
            elif body is not None and not isinstance(body, (str, bytes)):
                if self._body_function_handler:
                    body = self._body_function_handler(body)
                body = json.dumps(body)
                is_json = True
            body_list.append(body)

        if is_json:
            headers["Content-Type"] = "application/json"

        responses = []
        for url, body in zip(url_list, body_list):
            responses.append(asyncio.ensure_future(self._submit(method, url, headers, body)))
        return await asyncio.gather(*responses)

    async def _submit(self, method, url, headers, body):
        async with self._client_session.request(method, url, headers=headers, data=body, ssl=False) as future:
            return await future.read(), future.headers

    async def _handle_completed(self, event, response):
        data = []
        for body, headers in response:
            data.append(self._get_data(body, headers))

        new_event = self._user_fn_output_to_event(event, data)
        await self._do_downstream(new_event)

    def _get_data(self, data, headers):
        if (
            self.return_json
            or headers.get("content-type", "").lower() == "application/json"
        ) and isinstance(data, (str, bytes)):
            data = json.loads(data)
        return data

    def to_dict(self):
        args = {
            key: getattr(self, key)
            for key in [
                "url",
                "subpath",
                "method",
                "headers",
                "return_json",
                "url_expression",
                "body_expression",
            ]
            if getattr(self, key) is not None
        }
        return {
            "class_name": f"{__name__}.{self.__class__.__name__}",
            "name": self.name or self.__class__.__name__,
            "class_args": args,
            "input_path": self._input_path,
            "result_path": self._result_path,
        }
