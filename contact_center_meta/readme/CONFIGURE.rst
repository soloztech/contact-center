Create the Meta resources in the shared integration core:

#. Create one ``meta.api.app`` with the external App ID, Graph version and an App
   secret reference.
#. Create one ``meta.webhook.endpoint`` with a verify-token reference.
#. Create each ``meta.webhook.page`` with its Page-token reference and add linked
   Instagram assets when needed.
#. In *Contact Center > Configuration > Provider Connections*, select ``Meta`` and
   bind the connection to the exact messaging asset.
#. Click **Configure Meta Webhook** and wait for the endpoint and Page to report a
   fresh ``In Sync`` read-back.
#. Run the provider health check and then enable inbound/outbound traffic.

Credential fields contain environment-variable or mounted-file references only.
Changing an external route requires archiving the connection and creating another
one; the asset binding and logical account identity are immutable.
