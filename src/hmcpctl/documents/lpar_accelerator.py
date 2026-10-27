"""Typed native GZIP accelerator QoS request."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class LparAcceleratorQos(BaseModel):
    """Set an explicit QoS quantity; target-specific validation belongs to the HMC."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    qos: Annotated[StrictInt, Field(ge=0)] = Field(
        description="Explicit nonnegative GZIP accelerator QoS quantity to submit to the HMC."
    )
    wait_minutes: Annotated[StrictInt, Field(ge=0)] | None = Field(
        default=None,
        description="Minutes to wait for partition operating system commands; zero disables that native deadline. The SSH deadline still applies. Omit to use the HMC default.",
    )
    detail_level: Annotated[StrictInt, Field(ge=1, le=4)] | None = Field(
        default=None,
        description="Partition operating system command detail from 1 through 4. Omit to use the HMC default.",
    )
    force: StrictBool = Field(
        default=False,
        description="Request the native force option for an AIX, Linux, or VIOS partition without RMC. A successful change then requires an operating system restart.",
    )
