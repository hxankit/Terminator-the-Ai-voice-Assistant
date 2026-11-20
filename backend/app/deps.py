from fastapi import Depends
from .security import current_user, rate_limited

User = Depends(current_user)
ChatUser = Depends(rate_limited(30))
