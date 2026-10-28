"""One schema correction through the core's normal budgeted generation path."""
import json

import jsonschema

from myatri.providers import Gateway as CoreGateway, ProviderError
from myatri.providers.gateway import BudgetExceeded
from .vision import VisionProvider, image_tokens


class Gateway(CoreGateway):
    @classmethod
    def adapt(cls, gateway, *, vision_root=None):
        adapted = cls.__new__(cls)
        adapted.__dict__.update(gateway.__dict__)
        adapted.vision_root = vision_root
        if vision_root is not None and not isinstance(adapted.provider, VisionProvider):
            adapted.provider = VisionProvider(adapted.provider, vision_root)
        return adapted

    async def _generate(self, request):
        extra = image_tokens(request, self.vision_root) if getattr(self, 'vision_root', None) else 0
        if extra:
            estimated = (request.estimated_input_tokens if request.role == 'foreground'
                         and request.estimated_input_tokens is not None else
                         len(json.dumps(request.model_dump(mode='json'), ensure_ascii=False).encode('utf-8')))
            if estimated + request.max_tokens + extra > request.budget_tokens:
                raise BudgetExceeded('request with tool images exceeds token budget')
        return await super().generate(request)

    def _reserve(self, estimated, request):
        extra = image_tokens(request, self.vision_root) if getattr(self, 'vision_root', None) else 0
        return super()._reserve(estimated + extra, request)

    async def generate(self, request):
        try:
            return await self._generate(request)
        except ProviderError as exc:
            cause = exc.__cause__
            if not request.output_schema or not isinstance(cause, (json.JSONDecodeError, jsonschema.ValidationError)):
                raise
            details = {"validator": "JSONDecodeError", "path": [], "missing_required": []}
            if isinstance(cause, jsonschema.ValidationError):
                # Schema locations and required field names avoid copying model
                # output values or the validator's potentially private message.
                details.update(validator=cause.validator, path=list(cause.absolute_schema_path))
                if cause.validator == "required" and isinstance(cause.instance, dict):
                    details["missing_required"] = [key for key in cause.validator_value if key not in cause.instance]
            retry = request.model_copy(deep=True)
            retry.messages.append({"role": "user", "content":
                "上次JSON未通过校验：" + json.dumps(details, ensure_ascii=False) +
                "。请按原schema重新输出完整JSON。"})
            retry.purpose += "_schema_retry"
            # A cached foreground estimate excludes the appended instruction.
            retry.estimated_input_tokens = None
            return await self._generate(retry)
