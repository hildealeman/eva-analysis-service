from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ProfileStats(BaseModel):
    totalVotes: int = 0
    upvotes: int = 0
    downvotes: int = 0
    suggestionsMade: int = 0
    branchesOpened: int = 0
    branchesClosed: int = 0


class InvitationStats(BaseModel):
    invitationsAvailable: int = 3
    invitationsUsed: int = 0
    inviteesCount: int = 0


class Profile(BaseModel):
    id: str
    role: Literal["ghost", "active"]
    mode: Literal["passive", "active"]
    tevScore: float
    createdAt: str
    lastActiveAt: str
    stats: ProfileStats
    invitationStats: InvitationStats


class VotesSummary(BaseModel):
    upvotes: int = 0
    downvotes: int = 0


class ProgressSummary(BaseModel):
    profileId: str
    date: str
    progressTowardsActivation: float
    activitySeconds: int
    sessionCount: int
    votes: VotesSummary
    ethicalTrend: Literal["onTrack", "behind", "regressing"]
    canPromoteToActive: bool
