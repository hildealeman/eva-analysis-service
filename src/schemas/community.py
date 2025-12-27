from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class ProfileOut(BaseModel):
    id: str
    createdAt: str
    updatedAt: str

    role: Literal["ghost", "active"]
    state: Literal["ok", "suspended", "banned"]

    tevScore: float
    dailyStreak: int
    lastActiveAt: str

    invitationsGrantedTotal: int
    invitationsUsed: int
    invitationsRemaining: int


class VotesGivenOut(BaseModel):
    up: int = 0
    down: int = 0


class ProgressSummaryOut(BaseModel):
    date: str
    tevScoreStart: float
    tevScoreEnd: float
    tevDelta: float

    votesGiven: VotesGivenOut

    activityMinutes: int
    shardsReviewed: int
    shardsPublished: int

    levelLabel: str
    progressPercentToNextLevel: int


class InvitationOut(BaseModel):
    id: str
    createdAt: str
    updatedAt: str

    inviterId: str
    inviteeId: Optional[str] = None

    email: str
    code: str

    state: Literal["pending", "accepted", "revoked", "expired"]
    expiresAt: str

    acceptedAt: Optional[str] = None
    revokedAt: Optional[str] = None


class InvitationsSummaryOut(BaseModel):
    grantedTotal: int
    used: int
    remaining: int


class MeResponse(BaseModel):
    profile: ProfileOut
    todayProgress: Optional[ProgressSummaryOut] = None
    invitationsSummary: InvitationsSummaryOut


class MeProgressResponse(BaseModel):
    today: ProgressSummaryOut
    history: list[ProgressSummaryOut]


class MeInvitationsResponse(BaseModel):
    invitations: list[InvitationOut]


class CreateInvitationRequest(BaseModel):
    email: str


class CreateInvitationResponse(BaseModel):
    invitation: InvitationOut
